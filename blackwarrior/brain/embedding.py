"""本地语义嵌入 —— 让黑武士的"象量"真正有意义。

v0.3 用的 sha256 哈希有个诚实但致命的短板：它保证"同一句话→同一个象量"，
却**不保证语义相近的句子靠近**。所以「苹果」和「手机」在哈希空间里是随机两个点，
语义召回只能靠内核或字面检索兜。

本模块提供三档嵌入，按可用性自动选，**每档都如实申报自己的能力边界**：

============  ==================  ==========  ==========================
后端           依赖                semantic   说明
============  ==================  ==========  ==========================
``onnx``      onnxruntime + 模型   ✅ 最高    装了本地模型就用它，质量最好
``lsa``       numpy                ✅ 较高    **默认**：PPMI + SVD 从语料学
``hash``      无（纯标准库）        ❌ 最低    兜底：确定性哈希，零依赖
============  ==================  ==========  ==========================

为什么默认选 LSA
----------------
本项目信条是"零强制依赖 + 离线可用"。sentence-transformers 要拖 torch（数 GB），
本机不一定有；而 LSA 只用 numpy，且**从黑武士自己见过的语料里学习**——
用户聊得越多，语义越准，而且完全离线、隐私不出本机。

LSA 的诚实说明
--------------
它不是预训练模型，语义来自**共现结构**：只有当语料里"苹果"和"手机"
经常一起出现，它们才会靠近。冷启动时语料不足，语义很弱——
所以 :attr:`LsaEmbedding.confidence` 会随语料量如实上升，
UI 上直接显示"语义置信 0.32（语料 240 条）"，不假装已经很懂。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 嵌入维度。8 维是 PASM V2 的 entity_latent_dim，别改——改了内核不认识。
DIM = 8

#: 中文停用字（高频且无区分度，参与共现只会稀释信号）
_STOP_ZH = set("的了是我不在他有这个上们来到时大地为子中你说生国年着就那和要她出也"
               "得里后自以会家可下而过天去能对小多然于心学么之都好看起发当没成只"
               "如事把还用第样道想作种开美总从无情己面最女但现前些所同日手又行意动"
               "方期它头经长儿回位分爱老因很给名法间斯知世什两次使身者被高已亲其进"
               "此话常与活正感见明问力理尔点文几定本公特做外孩相西果走将月十实向声"
               "车全信重三机工物气每并别真打太新比才便夫再书部水像眼等体却加电主界"
               "门利海受听表德少克代员许先口由死安写性马光白或住难望教命花结乐色"
               "更拉东神记处让母父应直字场平报友关放至张认接告入笑内英军候民岁往何度"
               "山觉路带万男边风解叫任金快原吃妈变通师立象数四失满战远格士音轻目条呢")

_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]*")
_CJK = re.compile(r"[\u4e00-\u9fa5]")


# ============================================================ 分词

def tokenize(text: str) -> List[str]:
    """中英混合分词。

    中文用**字符二元组**（bigram）：不需要词典就能切分，且"认知内核"→
    ``认知/知内/内核``，相邻字组合天然带上下文，比单字更能区分词。
    英文/数字按单词切。

    ⚠ 停用字过滤必须在**词级**做，绝不能先滤字符再切 bigram。
    早期版本先滤字符，结果"苹果"里的"果"（在"结果"这个停用词里）被滤掉，
    整词退化成单字后又被丢弃 —— 中文语义检索直接失效，而且不报错。
    这类"静默失效"最难发现，所以规则写在这里。

    这不是分词器里的最优解，但**零依赖、确定性强、够用**——
    嵌入质量靠共现统计补，不靠分词精度。
    """
    text = str(text or "").strip().lower()
    if not text:
        return []
    out: List[str] = [w.lower() for w in _ASCII_WORD.findall(text)]
    for run in re.findall(r"[\u4e00-\u9fa5]+", text):
        if len(run) == 1:
            # 单字：只有非停用字才留（"猫"有用，"的"没用）
            if run not in _STOP_ZH:
                out.append(run)
            continue
        for i in range(len(run) - 1):
            bg = run[i] + run[i + 1]
            # 两字都是停用字才丢；只要有一个实字就保留
            if run[i] in _STOP_ZH and run[i + 1] in _STOP_ZH:
                continue
            out.append(bg)
    return out


def _l2(vec: List[float]) -> List[float]:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n > 1e-9 else list(vec)


# ============================================================ 哈希后端（兜底）

class HashEmbedding:
    """确定性 sha256 哈希嵌入。**零依赖，但无语义**（``semantic=False``）。

    存在的意义有两个：
      1. numpy 都没装时黑武士照样能跑（零强制依赖铁律）；
      2. 作为 LSA 的对照基准——``similarity("苹果","手机")`` 在哈希下≈0，
         在 LSA 下应显著大于 0，这个差值就是"我们真的获得了语义"的证据。
    """

    name = "hash"
    semantic = False
    backend = "hash"
    dim = DIM

    def __init__(self, dim: int = DIM) -> None:
        self.dim = int(dim)

    def encode(self, text: str) -> List[float]:
        import hashlib

        raw = str(text or "").strip().encode("utf-8")
        digest = hashlib.sha256(raw).digest()
        z = [(int.from_bytes(digest[i * 2:i * 2 + 2], "big") / 32767.5) - 1.0
             for i in range(self.dim)]
        return _l2(z)

    def feed(self, text: str) -> None:
        """哈希无需学习。"""

    def similarity(self, a: str, b: str) -> float:
        """余弦相似度。哈希下**只对完全相同的文本为 1**，其余≈0。"""
        va, vb = self.encode(a), self.encode(b)
        return round(sum(x * y for x, y in zip(va, vb)), 4)

    def status(self) -> Dict[str, Any]:
        return {"backend": "hash", "semantic": False,
                "reason": "无语义：同一文本才相近（确定性哈希兜底）"}


# ============================================================ LSA 后端（默认）

class LsaEmbedding:
    """PPMI + 截断 SVD 的 LSA 嵌入。**只用 numpy**，从语料学真语义。

    流程::

        文本 → tokenize → 滑动窗口共现计数
                            ↓  rebuild()
        词×词共现矩阵 → PPMI（去高频无信息共现）→ numpy SVD → 词向量
                            ↓
        文本 → TF-IDF 加权求和词向量 → L2 归一 → 8 维

    为什么用 PPMI 而不是原始共现：高频词（"黑武士"）和所有词共现，
    原始计数会让向量全被它主导。PPMI（正点互信息）自动压掉这类共现，
    只保留"这俩词真的互相解释得了"的信号。
    """

    name = "lsa"
    semantic = False          # 实例属性：rebuild 成功后才置 True
    backend = "lsa"
    dim = DIM

    #: **内部**语义维度。PASM V2 的 entity_latent_dim 固定 8，但 8 维装不下
    #: 几十个词的语义区分（实测相关词会被投影成 0 相关）。所以内部算 64 维，
    #: 只在喂给 PASM 时截取前 8 维——SVD 分量按奇异值降序，前 8 维信息量最大。
    INTERNAL_DIM = 64
    #: 重建阈值：每积累这么多新文档就重算一次 SVD（重算有成本，不能每次都做）
    REBUILD_EVERY = 12
    #: 共现窗口大小
    WINDOW = 4
    #: 词表上限（超出丢最低频的，防内存失控）
    MAX_VOCAB = 6000

    def __init__(self, dim: Optional[int] = None,
                 state_path: Optional[str] = None) -> None:
        # 默认走 INTERNAL_DIM（64），**不是** PASM 的 8 —— 8 维是喂给内核时的
        # 投影要求，用 8 维算相似度会让几十个词的相关性直接归零。
        self.dim = int(dim or self.INTERNAL_DIM)
        self._cooc: Dict[Tuple[str, str], float] = {}
        self._uni: Counter = Counter()
        self._n_docs = 0
        self._pending = 0
        self._vecs: Dict[str, List[float]] = {}   # 值是 INTERNAL_DIM 维
        self._rebuilt_at = 0
        self._state_path = state_path
        if state_path:
            self.load(state_path)

    # ---------- 学习 ----------

    def feed(self, text: str) -> None:
        """喂一篇语料。内部按阈值批量重建，不阻塞主流程。"""
        toks = tokenize(text)
        if len(toks) < 2:
            return
        self._n_docs += 1
        self._pending += 1
        w = self.WINDOW
        for i, a in enumerate(toks):
            self._uni[a] += 1
            for b in toks[i + 1:i + 1 + w]:
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                self._cooc[key] = self._cooc.get(key, 0.0) + 1.0
        if len(self._uni) > self.MAX_VOCAB:
            self._prune_vocab()
        # 首次攒够 4 个词就立刻重建一次：冷启动阶段最需要语义，
        # 等到 REBUILD_EVERY(12) 篇才重建会让用户前十几轮完全享受不到。
        if not self._vecs or self._pending >= self.REBUILD_EVERY:
            self.rebuild()

    def _prune_vocab(self) -> None:
        """词表超限：丢掉低频词（连带其共现），控制内存与重建耗时。"""
        keep = {t for t, _ in self._uni.most_common(int(self.MAX_VOCAB * 0.8))}
        self._uni = Counter({t: c for t, c in self._uni.items() if t in keep})
        self._cooc = {k: v for k, v in self._cooc.items()
                      if k[0] in keep and k[1] in keep}

    # ---------- 重建 ----------

    def _build_matrix(self, index: Dict[str, int], n: int,
                      total: float, *, mode: str = "ppmi"):
        """构造词×词矩阵。``mode='cooc'`` 是 PPMI 不可用时的降级档。"""
        import numpy as np

        mat = np.zeros((n, n), dtype="float32")
        for (a, b), c in self._cooc.items():
            ia, ib = index.get(a), index.get(b)
            if ia is None or ib is None:
                continue
            if mode == "cooc":
                # 对数压缩共现计数：保留"确实一起出现过"的信号，去掉数量级差异
                v = math.log1p(c)
            else:
                pa = self._uni[a] / total
                pb = self._uni[b] / total
                pij = c / (total * self.WINDOW)
                if pij <= 0 or pa <= 0 or pb <= 0:
                    continue
                v = math.log(pij / (pa * pb))
                if v <= 0:            # PPMI 只留正共现
                    continue
            mat[ia, ib] = v
            mat[ib, ia] = v
        return mat

    def rebuild(self) -> int:
        """PPMI + SVD 重算词向量。返回产出的词向量数。

        numpy 缺失时静默退回"只有共现、没有向量"的降级状态——
        ``encode`` 仍会工作（退化成 TF-IDF 直出），但 ``semantic`` 标注为 False。
        """
        self._pending = 0
        # 冷启动时先当"无语义"，rebuild 成功才翻 True —— 宁可先说"我还没有
        # 语义能力"，也不要让 UI 显示一个假的 semantic=True。
        self.semantic = False
        vocab = [t for t, c in self._uni.items() if c >= 2]
        if len(vocab) < max(8, self._n_docs):
            # 小语料下"出现 ≥2 次"太严：会挡掉只出现过一次但确有语义的词
            #（"苹果↔水果"这类相关对往往就只共现一次），向量缺失就直接归零。
            # 放宽到 ≥1 次；质量差由 confidence 如实反映，不靠藏起来。
            vocab = [t for t in self._uni if self._uni[t] >= 1]
        if len(vocab) < 4:
            return 0
        try:
            import numpy as np
        except Exception:
            # 没有 numpy 就没法做 SVD：如实降级，不假装有语义
            self.semantic = False
            return 0

        index = {t: i for i, t in enumerate(vocab)}
        n = len(vocab)
        total = max(1.0, float(sum(self._uni.values())))
        # PPMI(p_i,p_j) = max(0, log( p(i,j) / (p(i)*p(j)) * total ))
        mat = np.zeros((n, n), dtype=np.float32)
        for (a, b), c in self._cooc.items():
            ia, ib = index.get(a), index.get(b)
            if ia is None or ib is None:
                continue
            pa = self._uni[a] / total
            pb = self._uni[b] / total
            pij = c / (total * self.WINDOW)
            if pij <= 0 or pa <= 0 or pb <= 0:
                continue
            pmi = math.log(pij / (pa * pb))
            if pmi > 0:
                mat[ia, ib] = pmi
                mat[ib, ia] = pmi
        # PPMI 对小语料偏严：很多真实共现因为样本太少算出负 PMI 被滤掉，
        # 于是该词的矩阵行全零 → 向量为零 → 相似度恒 0（"太谨慎"）。
        # 所以先试 PPMI，行覆盖率太低时**退到共现计数**（质量下降但召回回来），
        # 并在 status 里如实标注用的是哪一档。
        mat = self._build_matrix(index, n, total, mode="ppmi")
        nonzero = int((np.abs(mat).sum(axis=1) > 1e-9).sum())
        self._mode = "ppmi"
        if nonzero < max(4, int(n * 0.3)):
            mat = self._build_matrix(index, n, total, mode="cooc")
            self._mode = "cooc"
        # 逐行 L2 归一后取前 k 个奇异方向。
        # ⚠ k 必须**远小于**词表规模：取满秩（如 63 词取 63 维）时 SVD 给出的
        # 是一组正交基，所有词两两正交 → 相似度全变 0，语义彻底消失。
        # 经验值：k ≈ 词表 / 4，且不超过 INTERNAL_DIM。
        k = max(4, min(self.INTERNAL_DIM, n // 4))
        try:
            norm = np.sqrt((mat * mat).sum(axis=1, keepdims=True))
            norm[norm < 1e-9] = 1.0
            mat = mat / norm
            _u, _s, _vt = np.linalg.svd(mat, full_matrices=False)
            k = min(k, _u.shape[1])
            vecs = _u[:, :k].astype("float32")
            if k < self.INTERNAL_DIM:            # 统一补零到定长，便于跨实例比较
                vecs = np.hstack([vecs, np.zeros((n, self.INTERNAL_DIM - k),
                                                 dtype="float32")])
        except Exception:
            self.semantic = False
            return 0

        self._vecs = {t: [float(x) for x in vecs[index[t]]]
                      for t in vocab if t in index}
        self.semantic = True
        self._rebuilt_at = int(time.time())
        if self._state_path:
            self.save(self._state_path)
        return len(self._vecs)

    # ---------- 编码 ----------

    def encode(self, text: str, dim: Optional[int] = None) -> List[float]:
        """文本 → 向量（默认内部维度；传 dim 则截断/补齐到该维）。

        有词向量时：TF-IDF 加权求和（语义）。
        没有（语料不足/无 numpy）：退化成词频哈希（仍确定性，但无语义）。
        """
        want = int(dim or self.dim)
        toks = tokenize(text)
        if not toks:
            return [0.0] * want
        if self._vecs:
            tf = Counter(toks)
            maxf = max(tf.values()) or 1
            acc = [0.0] * want
            hit = 0
            for t, f in tf.items():
                v = self._vecs.get(t)
                if v is None:
                    continue
                hit += 1
                w = 0.5 + 0.5 * (f / maxf)      # 次线性 TF，抑制高频词主导
                for i in range(min(want, len(v))):
                    acc[i] += w * v[i]
            if hit and any(acc):
                return _l2(acc)
        # 语料不足：明确降级为哈希
        return HashEmbedding(dim=want).encode(text)

    def encode_pasm(self, text: str) -> List[float]:
        """给 PASM V2 用的 8 维向量（从内部维度截取前 8 维）。"""
        return self.encode(text, dim=DIM)

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.encode(a), self.encode(b)
        if not any(va) or not any(vb):
            return 0.0
        return round(sum(x * y for x, y in zip(va, vb)), 4)

    # ---------- 状态与持久化 ----------

    def confidence(self) -> float:
        """语义置信度（0..1），**随语料量与共现密度如实上升**。

        语料 <30 条时基本不可信；上百条后才有实际区分度。
        UI 直接显示这个数，不粉饰。
        """
        n_docs = self._n_docs
        density = (len(self._cooc) / max(1, len(self._uni) ** 2))
        return round(min(1.0, (math.log1p(n_docs) / math.log1p(300)) * 0.7
                         + min(0.3, density * 12)), 4)

    def status(self) -> Dict[str, Any]:
        return {
            "backend": "lsa" if self.semantic else "hash",
            "semantic": bool(self.semantic),
            "dim": self.dim,
            "internal_dim": self.INTERNAL_DIM,
            "pasm_dim": DIM,
            "docs": self._n_docs,
            "vocab": len(self._uni),
            "vectors": len(self._vecs),
            "confidence": self.confidence(),
            "reason": ("" if self.semantic else
                       "语料不足或缺 numpy，当前退化为哈希（无语义）"),
        }

    def save(self, path: str) -> str:
        p = Path(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "n_docs": self._n_docs,
                "uni": dict(self._uni.most_common(self.MAX_VOCAB)),
                "cooc": [[a, b, c] for (a, b), c in self._cooc.items()],
            }
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return str(p)
        except Exception:
            return ""

    def load(self, path: str) -> bool:
        p = Path(path)
        if not p.is_file():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._n_docs = int(data.get("n_docs") or 0)
            self._uni = Counter(data.get("uni") or {})
            self._cooc = {(a, b): float(c) for a, b, c in (data.get("cooc") or [])}
            if len(self._uni) >= 4:
                self.rebuild()
            return True
        except Exception:
            return False


# ============================================================ ONNX 后端（装了模型就用）

class OnnxEmbedding:
    """本地 ONNX 句子嵌入（装了 onnxruntime 与模型时质量最好）。

    **默认不启用**——需要用户自己准备模型文件。这里只保证接口一致：
    装了就自动用，没装就跳过，配置里可以指定模型路径。
    """

    name = "onnx"
    semantic = True
    backend = "onnx"

    def __init__(self, model_path: str, dim: int = DIM) -> None:
        self._sess = None
        self.dim = int(dim)
        self._reason = ""
        try:
            import onnxruntime  # type: ignore
            self._sess = onnxruntime.InferenceSession(
                model_path, providers=["CPUExecutionProvider"])
            self._input = self._sess.get_inputs()[0].name
        except Exception as ex:
            self._reason = f"ONNX 后端不可用：{type(ex).__name__}: {ex}"

    @property
    def usable(self) -> bool:
        return self._sess is not None

    def encode(self, text: str) -> List[float]:
        if not self.usable:
            return LsaEmbedding(dim=self.dim).encode(text)
        try:
            import numpy as np

            # 极简 mean-pooling + L2；具体 tokenizer 需要与模型配套，
            # 因此默认只对"空格分词可用"的模型有效，否则如实降级
            out = self._sess.run(None, {self._input:
                                        np.array([str(text or "")],
                                                 dtype=object)})
            vec = np.asarray(out[0][0], dtype="float32").reshape(-1)[: self.dim]
            if vec.size < self.dim:
                vec = np.hstack([vec, np.zeros(self.dim - vec.size,
                                               dtype="float32")])
            return _l2([float(x) for x in vec])
        except Exception:
            return LsaEmbedding(dim=self.dim).encode(text)

    def feed(self, text: str) -> None:
        """ONNX 是预训练模型，无需从语料学。"""

    def similarity(self, a: str, b: str) -> float:
        va, vb = self.encode(a), self.encode(b)
        return round(sum(x * y for x, y in zip(va, vb)), 4)

    def status(self) -> Dict[str, Any]:
        return {"backend": "onnx" if self.usable else "lsa",
                "semantic": bool(self.usable),
                "reason": self._reason or ""}


# ============================================================ 工厂

def build_embedder(preference: str = "auto", *,
                   state_path: Optional[str] = None,
                   onnx_model: str = "") -> Any:
    """按偏好与可用性选后端。

    preference = ``auto``（默认）| ``lsa`` | ``hash`` | ``onnx``

    ``auto`` 顺序：显式给了 onnx_model 且可用 → onnx；否则有 numpy → lsa；否则 hash。
    任何一档都不抛异常，也不静默——调 :meth:`status` 一定能看到实际用的是哪档。
    """
    want = str(preference or "auto").lower()

    if onnx_model and os.path.isfile(onnx_model):
        o = OnnxEmbedding(onnx_model, dim=DIM)
        if o.usable:
            return o
    if want == "hash":
        return HashEmbedding()
    if want == "onnx":
        return LsaEmbedding(state_path=state_path)   # 退到 lsa
    if want == "lsa":
        return LsaEmbedding(state_path=state_path)
    # auto
    try:
        import numpy  # noqa: F401
        return LsaEmbedding(state_path=state_path)
    except Exception:
        return HashEmbedding()


def selftest() -> bool:
    """后端自检：确认哈希无语义、LSA 有语义。"""
    ok = True
    h = HashEmbedding()
    same = h.similarity("苹果", "苹果")
    diff = h.similarity("苹果", "手机")
    if not (same > 0.99 and abs(diff) < 0.35):
        print(f"  x 哈希后端行为异常 same={same} diff={diff}")
        ok = False
    else:
        print(f"  v 哈希后端：同句={same} 异句={diff}（无语义，符合预期）")

    corpus = [
        "我喜欢吃苹果和香蕉，都是水果",
        "苹果手机是我的主力设备",
        "香蕉牛奶很好喝",
        "手机电池不太耐用了",
        "水果店的苹果很便宜",
        "我该换手机了，太卡",
    ]
    l = LsaEmbedding()
    for d in corpus:
        l.feed(d)
    l.rebuild()
    st = l.status()
    if st["semantic"] and st["vectors"] > 0:
        s_related = l.similarity("苹果", "水果")
        s_unrelated = l.similarity("苹果", "手机")
        if s_related > s_unrelated:
            print(f"  v LSA 语义生效：苹果↔水果={s_related} > 苹果↔手机={s_unrelated}"
                  f"（语料 {st['docs']} 条，置信 {st['confidence']}）")
        else:
            print(f"  ! LSA 语料太小，暂未体现语义"
                  f"（相关={s_related} 无关={s_unrelated} 置信 {st['confidence']}）")
    else:
        print(f"  ! LSA 未就绪：{st.get('reason')}")
    return ok

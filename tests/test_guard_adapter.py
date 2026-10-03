"""A217 —— GuardModelAdapter(本地守卫模型适配器)测试。

全部离线:绝不安装/导入真实 transformers/torch,绝不下载模型。
沿用 test_hf_clip_adapter 的手法:
- 注入 FakeGuardPipeline(协议 ``__call__(image, prompt)``)驱动分类逻辑;
- monkeypatch 伪 transformers 模块驱动真实加载路径(捕获 pipeline 构造参数,
  断言 local_files_only=True 的禁网红线);
- Pillow 缺失的环境里注入最小假 PIL(仅 open/convert),使图片读取路径可测。

工厂路径(classifier_base._LAZY_IMPORT_MODULES 收录 "guard" 后
get_classifier("guard", cfg) 全链):清空注册表 + sys.modules 逼出真实惰性
导入,子进程验证新解释器全链零三方依赖。
"""
from __future__ import annotations

import errno
import logging
import pathlib
import struct
import subprocess
import sys
import threading
import time
import types
import zlib

import pytest

from netsentinel import telemetry
from netsentinel.contracts import Config, ImageEvidence

# 兄弟模块 classifier_base(A05)并行开发中,可能尚未就位:importorskip 容错。
classifier_base = pytest.importorskip("netsentinel.vision.classifier_base")
guard_mod = pytest.importorskip("netsentinel.vision.guard_adapter")
GuardModelAdapter = guard_mod.GuardModelAdapter
parse_yes_no = guard_mod.parse_yes_no
parse_llamaguard = guard_mod.parse_llamaguard
coerce_model_text = guard_mod.coerce_model_text
detect_family = guard_mod.detect_family


# ---------------------------------------------------------------------------
# 测试素材(与 test_hf_clip_adapter 同源手法)
# ---------------------------------------------------------------------------

def write_png(path, rgb: tuple[int, int, int] = (255, 0, 0),
              width: int = 1, height: int = 1) -> pathlib.Path:
    """用 stdlib zlib+struct 手写一个真实的小 PNG(默认 1x1 纯红)。"""
    path = pathlib.Path(path)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data)))

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + ihdr
           + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
    path.write_bytes(png)
    return path


def make_evidence(path) -> ImageEvidence:
    """由本地文件路径构造一条受检图片证据。"""
    return ImageEvidence(
        path=str(path),
        url="http://127.0.0.1/img/x.png",
        source_page="http://127.0.0.1/",
    )


class FakeGuardPipeline:
    """守卫 pipeline 替身:记录 (image, prompt) 调用,返回给定文本或抛异常。"""

    def __init__(self, output=None, error: Exception | None = None):
        self.output = output
        self.error = error
        self.calls = 0
        self.seen_prompts: list[str] = []
        self.seen_images: list[object] = []

    def __call__(self, image, prompt: str):
        self.calls += 1
        self.seen_images.append(image)
        self.seen_prompts.append(prompt)
        if self.error is not None:
            raise self.error
        return self.output


def _real_pil_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture
def ensure_pil(monkeypatch):
    """保证 classify() 内的 ``from PIL import Image`` 可用(真 PIL 优先,缺失注入假 PIL)。"""
    if _real_pil_available():
        return "real"

    class _UnidentifiedImageError(OSError):
        pass

    class _FakeImageFile:
        def __init__(self, path: str):
            self.path = path

        def convert(self, mode: str):
            if mode != "RGB":
                raise ValueError(f"假 PIL 不支持的转换模式: {mode}")
            return self

    def _open(path):
        p = pathlib.Path(path)
        if not p.is_file():
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))
        if not p.read_bytes().startswith(b"\x89PNG"):
            raise _UnidentifiedImageError(f"cannot identify image file {str(path)!r}")
        return _FakeImageFile(str(path))

    fake_pil = types.ModuleType("PIL")
    fake_image = types.ModuleType("PIL.Image")
    fake_image.open = _open
    fake_image.UnidentifiedImageError = _UnidentifiedImageError
    fake_pil.Image = fake_image
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image)
    return "fake"


def install_fake_transformers(monkeypatch, raw_output=None):
    """向 sys.modules 注入伪 transformers 模块,pipeline 构造参数全量捕获。"""
    created: list = []

    class _FakeRawPipeline:
        def __init__(self, task, **kwargs):
            self.task = task
            self.kwargs = dict(kwargs)
            self.payloads: list[dict] = []
            created.append(self)

        def __call__(self, payload):
            self.payloads.append(payload)
            if isinstance(raw_output, Exception):
                raise raw_output
            return raw_output

    fake_tx = types.ModuleType("transformers")
    fake_tx.pipeline = _FakeRawPipeline
    monkeypatch.setitem(sys.modules, "transformers", fake_tx)
    return created


# ---------------------------------------------------------------------------
# 注册与契约
# ---------------------------------------------------------------------------

def test_registered_as_guard():
    """模块导入时已把 GuardModelAdapter 注册为 "guard"(NsfwClassifier 契约)。"""
    assert issubclass(GuardModelAdapter, classifier_base.NsfwClassifier)
    assert GuardModelAdapter.name == "guard"
    with classifier_base._REGISTRY_LOCK:
        assert classifier_base._REGISTRY.get("guard") is GuardModelAdapter


def test_guard_families_registry_consistency():
    """族枚举与提示词/任务/解析器映射逐族咬合,缺一不可。"""
    for family in guard_mod.GUARD_FAMILIES:
        assert family in guard_mod.FAMILY_TASKS, family
        assert family in guard_mod.FAMILY_PARSERS, family
        assert callable(guard_mod.FAMILY_PARSERS[family]), family
    # 前两族内置提示词非空;custom-prompt 无内置(必须显式给)。
    assert guard_mod.FAMILY_PROMPTS["shieldgemma2"]
    assert guard_mod.FAMILY_PROMPTS["llamaguard"]
    assert guard_mod.FAMILY_PROMPTS["custom-prompt"] is None
    assert set(guard_mod.FAMILY_TASKS) == set(guard_mod.GUARD_FAMILIES)


def test_catalog_guard_ids_detectable_family():
    """model_catalog 的 guard 族条目与适配器的族探测互相对账。"""
    catalog = pytest.importorskip("netsentinel.vision.model_catalog")
    entries = catalog.MODELS.get("guard", [])
    assert entries, "model_catalog 未注册 guard 族"
    for info in entries:
        family = detect_family(info.id)
        assert family in ("shieldgemma2", "llamaguard"), info.id
        assert family in guard_mod.GUARD_FAMILIES


def test_zero_import_side_effects_subprocess():
    """子进程零导入副作用:阻断 transformers/torch/PIL 后模块仍可导入。"""
    code = (
        "import sys\n"
        "for name in ('transformers', 'torch', 'PIL', 'PIL.Image'):\n"
        "    sys.modules[name] = None\n"
        "import netsentinel.vision.guard_adapter as g\n"
        "assert g.GuardModelAdapter.name == 'guard'\n"
        "print('OK')\n"
    )
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(repo_root),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# 工厂路径:classifier_base._LAZY_IMPORT_MODULES 收录 "guard" 后
# get_classifier("guard", cfg) 全链打通(无需显式 import guard_adapter)
# ---------------------------------------------------------------------------

def _drop_guard_registration():
    """摘除 "guard" 注册表条目并返回原类(测试 finally 恢复);

    配合 ``monkeypatch.delitem(sys.modules, ...)`` 一并清空模块缓存,逼出
    get_classifier 的**真实惰性导入**路径(重导入触发模块级自注册)。
    """
    with classifier_base._REGISTRY_LOCK:
        return classifier_base._REGISTRY.pop("guard", None)


def test_lazy_import_map_contains_guard_entry():
    """惰性导入映射收录 "guard"(A217 工厂路径打通):条目结构与既有条目
    一致(注册名 → 兄弟模块路径字符串)。"""
    assert classifier_base._LAZY_IMPORT_MODULES.get("guard") == (
        "netsentinel.vision.guard_adapter"
    )
    for module_path in classifier_base._LAZY_IMPORT_MODULES.values():
        assert isinstance(module_path, str) and module_path.startswith(
            "netsentinel."
        ), module_path


def test_get_classifier_guard_lazy_import_and_cfg_injection(
    tmp_path, monkeypatch
):
    """工厂全链:清空注册后 get_classifier("guard", cfg) 经惰性导入自注册
    并实例化;``cfg.guard_model_path`` 注入路径命中真实加载(伪 transformers
    捕获构造参数:本地目录 + local_files_only=True 禁网红线不因工厂旁路)。"""
    original = _drop_guard_registration()
    try:
        monkeypatch.delitem(
            sys.modules, "netsentinel.vision.guard_adapter", raising=False
        )
        model_dir = tmp_path / "shieldgemma-2-4b-it"
        model_dir.mkdir()
        created = install_fake_transformers(
            monkeypatch, raw_output=[{"generated_text": "No"}]
        )
        cfg = Config()
        cfg.guard_model_path = str(model_dir)  # type: ignore[attr-defined]
        clf = classifier_base.get_classifier("guard", cfg)

        assert clf.name == "guard"
        assert isinstance(clf, classifier_base.NsfwClassifier)
        assert type(clf).__name__ == "GuardModelAdapter"
        with classifier_base._REGISTRY_LOCK:
            assert classifier_base._REGISTRY.get("guard") is type(clf)  # 惰性导入自注册
        assert len(created) == 1
        assert created[0].task == "image-text-to-text"
        assert created[0].kwargs.get("model") == str(model_dir)  # cfg 注入路径命中
        assert created[0].kwargs.get("local_files_only") is True  # 禁网不旁路
    finally:
        if original is not None:
            with classifier_base._REGISTRY_LOCK:
                classifier_base._REGISTRY["guard"] = original


def test_get_classifier_guard_missing_path_chinese_error(monkeypatch):
    """缺省错误语义:cfg 未注入 guard_model_path → 工厂路径抛适配器自身的
    中文 ValueError(与直接构造 GuardModelAdapter() 同文案);transformers
    被阻断仍到得了构造期 → 惰性导入零三方副作用。"""
    original = _drop_guard_registration()
    try:
        monkeypatch.delitem(
            sys.modules, "netsentinel.vision.guard_adapter", raising=False
        )
        monkeypatch.setitem(sys.modules, "transformers", None)
        with pytest.raises(ValueError, match="无法从模型路径推断守卫模型族"):
            classifier_base.get_classifier("guard", Config())
        with classifier_base._REGISTRY_LOCK:
            assert "guard" in classifier_base._REGISTRY  # 惰性导入已触发注册
    finally:
        if original is not None:
            with classifier_base._REGISTRY_LOCK:
                classifier_base._REGISTRY["guard"] = original


def test_get_classifier_guard_fresh_interpreter_lazy_path():
    """子进程全链:新解释器里 guard 未预注册 → get_classifier("guard") 经
    惰性导入映射触发注册(阻断 transformers/torch/PIL 依然畅通);缺省路径
    抛适配器中文 ValueError——工厂路径端到端可用、零三方依赖。"""
    code = (
        "import sys\n"
        "for name in ('transformers', 'torch', 'PIL', 'PIL.Image'):\n"
        "    sys.modules[name] = None\n"
        "from netsentinel.contracts import Config\n"
        "from netsentinel.vision import classifier_base as cb\n"
        "assert 'guard' not in cb._REGISTRY, '新进程不应预注册 guard'\n"
        "try:\n"
        "    cb.get_classifier('guard', Config())\n"
        "except ValueError as exc:\n"
        "    assert '无法从模型路径推断守卫模型族' in str(exc), str(exc)\n"
        "else:\n"
        "    raise AssertionError('缺省路径应抛中文 ValueError')\n"
        "assert 'guard' in cb._REGISTRY, '惰性导入应已触发注册'\n"
        "print('OK')\n"
    )
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(repo_root),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


# ---------------------------------------------------------------------------
# 解析器真值表(纯函数)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_verdict", [
    ("Yes", "unsafe"),
    ("yes", "unsafe"),
    ("  YES.  ", "unsafe"),
    ("Y", "unsafe"),
    ("No", "safe"),
    ("no", "safe"),
    ("\"No\"", "safe"),
    ("n.", "safe"),
    ("No\n(bonus line ignored)", "safe"),
])
def test_parse_yes_no_truth_table(text, expected_verdict):
    """Yes → 不安全高置信;No(含大小写/标点/空白变体)→ 安全;取首个非空行。"""
    prob, meta = parse_yes_no(text)
    assert meta["verdict"] == expected_verdict
    assert prob == (guard_mod.PROB_UNSAFE if expected_verdict == "unsafe"
                    else guard_mod.PROB_SAFE)


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "maybe", "安全", "Yes maybe", "0.97"])
def test_parse_yes_no_malformed_to_conservative_zero(text):
    """畸形输出:保守 0 分 + verdict=unparseable + 中文 error(对齐全库错误惯例)。"""
    prob, meta = parse_yes_no(text)
    assert prob == 0.0
    assert meta["verdict"] == "unparseable"
    assert "无法解析" in meta["error"]
    assert "期望 Yes/No" in meta["error"]


@pytest.mark.parametrize("text,expected_cats", [
    ("safe", []),
    ("Safe\n", []),
    ("safe\n(junk ignored)", []),
    ("unsafe\nS5", ["S5"]),
    ("Unsafe\nS1", ["S1"]),
    ("unsafe\nS1, S10", ["S1", "S10"]),
    ("unsafe\nS5\nS5", ["S5"]),          # 去重
    ("unsafe\ns14, s2", ["S14", "S2"]),  # 类别码大小写归一
    ("unsafe", []),                      # 无类别码:判定仍是不安全
    ("unsafe\nnot-a-code", []),
])
def test_parse_llamaguard_truth_table(text, expected_cats):
    """safe → 低分;unsafe → 高分 + 类别码序列按序去重收集。"""
    prob, meta = parse_llamaguard(text)
    assert meta["categories"] == expected_cats
    if meta["verdict"] == "unsafe":
        assert prob == guard_mod.PROB_UNSAFE
    else:
        assert prob == guard_mod.PROB_SAFE
        assert meta["verdict"] == "safe"


@pytest.mark.parametrize("text", ["", "   ", "definitely safe", "S5", "不安全"])
def test_parse_llamaguard_malformed_to_conservative_zero(text):
    """畸形输出(首行非 safe/unsafe):保守 0 分 + 中文 error。"""
    prob, meta = parse_llamaguard(text)
    assert prob == 0.0
    assert meta["verdict"] == "unparseable"
    assert "期望 safe/unsafe" in meta["error"]


@pytest.mark.parametrize("output,expected", [
    ("No", "No"),                                          # 字符串直传
    ({"generated_text": "Yes"}, "Yes"),                    # dict 键命中
    ({"output_text": "No"}, "No"),
    ([{"generated_text": "No"}], "No"),                    # transformers 列表形态
    ([{"generated_text": "Yes"}, {"generated_text": "No"}], "Yes"),  # 取首条
    (({"text": "No"},), "No"),                             # tuple 形态
    ([{"generated_text": ["No"]}], "No"),                  # 嵌套列表
    (None, ""),                                            # None → 空串
    ([], ""),                                              # 空列表 → 空串
    ({"unknown_key": "No"}, ""),                           # 无命中键 → 空串
    (42, "42"),                                            # 其余对象 str() 化
])
def test_coerce_model_text_truth_table(output, expected):
    """多种 pipeline 返回形态统一归一为生成文本(确定性纯函数)。"""
    assert coerce_model_text(output) == expected


@pytest.mark.parametrize("path,expected", [
    ("models/shieldgemma-2-4b-it", "shieldgemma2"),
    ("Models/ShieldGemma-2-4B-IT", "shieldgemma2"),        # 大小写不敏感
    ("meta-llama/Llama-Guard-3-11B-vision", "llamaguard"),
    ("llama-guard3", "llamaguard"),
    ("meta-llama/Llama-Guard-4-12B", "llamaguard"),
    ("models/qwen2.5-vl", None),
    ("", None),
    ("guard-only", None),                                  # 有 guard 无 llama,推断不出
])
def test_detect_family_truth_table(path, expected):
    assert detect_family(path) == expected


# ---------------------------------------------------------------------------
# 构造参数与错误路径(中文报错)
# ---------------------------------------------------------------------------

def test_unknown_family_raises_chinese():
    with pytest.raises(ValueError, match="未知的守卫模型族.*shieldgemma2/llamaguard"):
        GuardModelAdapter(family="shieldgemma", pipeline=FakeGuardPipeline("No"))


def test_family_undetectable_raises_chinese():
    with pytest.raises(ValueError, match="无法从模型路径推断守卫模型族"):
        GuardModelAdapter(model_path="models/qwen", pipeline=FakeGuardPipeline("No"))


def test_custom_prompt_requires_template():
    with pytest.raises(ValueError, match="custom-prompt 族必须显式提供"):
        GuardModelAdapter(family="custom-prompt", pipeline=FakeGuardPipeline("No"))


def test_missing_model_dir_raises_chinese(tmp_path, monkeypatch):
    """模型路径不存在:中文 FileNotFoundError;且先于 transformers 导入(路径校验在前)。"""
    missing = tmp_path / "no-such-guard-model"
    # 阻断 transformers:若适配器在路径校验前就触碰依赖,这里会抛 ImportError
    # 而非 FileNotFoundError——断言因此同时锁定"报错文案"与"零依赖导入"两件事。
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(FileNotFoundError, match="守卫模型本地目录不存在.*绝不联网下载"):
        GuardModelAdapter(model_path=str(missing), family="shieldgemma2")


def test_empty_model_path_no_pipeline_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="守卫模型本地目录不存在"):
        GuardModelAdapter(model_path="", family="llamaguard")


def test_import_error_hint_when_transformers_missing(tmp_path, monkeypatch):
    """transformers 缺失(sys.modules 置 None 阻断):中文安装提示,对齐 hf_clip。"""
    model_dir = tmp_path / "shieldgemma-2-4b-it"
    model_dir.mkdir()
    monkeypatch.setitem(sys.modules, "transformers", None)
    with pytest.raises(ImportError, match=r"未安装 transformers/torch.*netsentinel\[clip\]"):
        GuardModelAdapter(model_path=str(model_dir))


def test_import_error_hint_when_pil_missing(tmp_path, ensure_pil, monkeypatch):
    """Pillow 缺失:classify 时抛中文提示(模型侧用注入替身,离线)。"""
    png = write_png(tmp_path / "g.png")
    clf = GuardModelAdapter(family="shieldgemma2",
                            pipeline=FakeGuardPipeline("No"))
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    with pytest.raises(ImportError, match="未安装 Pillow"):
        clf.classify(make_evidence(png))


# ---------------------------------------------------------------------------
# 真实加载路径:伪 transformers + local_files_only 禁网断言
# ---------------------------------------------------------------------------

def test_load_uses_local_dir_and_local_files_only(tmp_path, monkeypatch):
    """加载红线:任务名正确、model=本地目录、local_files_only=True(禁自动联网)。"""
    model_dir = tmp_path / "shieldgemma-2-4b-it"
    model_dir.mkdir()
    created = install_fake_transformers(monkeypatch, raw_output=[{"generated_text": "No"}])
    clf = GuardModelAdapter(model_path=str(model_dir))
    assert clf.family == "shieldgemma2"  # 由路径自动探测
    assert len(created) == 1
    raw = created[0]
    assert raw.task == "image-text-to-text"
    assert raw.kwargs.get("model") == str(model_dir)
    assert raw.kwargs.get("local_files_only") is True


def test_load_llamaguard_family_task(tmp_path, monkeypatch):
    model_dir = tmp_path / "Llama-Guard-3-11B-vision"
    model_dir.mkdir()
    created = install_fake_transformers(monkeypatch)
    clf = GuardModelAdapter(model_path=str(model_dir))
    assert clf.family == "llamaguard"
    assert created[0].task == "image-text-to-text"


def test_real_wrapper_payload_shape_and_end_to_end(tmp_path, monkeypatch, ensure_pil):
    """真实加载薄包装:统一 (image, prompt) 协议 → {"images","text"} 载荷,端到端可分。"""
    model_dir = tmp_path / "shieldgemma-2-4b-it"
    model_dir.mkdir()
    png = write_png(tmp_path / "a.png")
    created = install_fake_transformers(
        monkeypatch, raw_output=[{"generated_text": "Yes"}])
    clf = GuardModelAdapter(model_path=str(model_dir))
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == guard_mod.PROB_UNSAFE  # Yes → 不安全
    payload = created[0].payloads[0]
    assert set(payload) == {"images", "text"}
    assert payload["text"] == guard_mod.SHIELDGEMMA2_PROMPT


# ---------------------------------------------------------------------------
# 分类全链(注入替身,离线)
# ---------------------------------------------------------------------------

def test_classify_shieldgemma2_yes_no(tmp_path, ensure_pil):
    """shieldgemma2 族:No → 低分;Yes → 高分;scores 记录族/原文本/verdict。"""
    png = write_png(tmp_path / "a.png")
    pipe_no = FakeGuardPipeline("No")
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=pipe_no)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == guard_mod.PROB_SAFE
    assert score.model == "guard"
    assert score.scores["verdict"] == "safe"
    assert score.scores["raw_text"] == "No"
    assert score.scores["family"] == "shieldgemma2"
    assert pipe_no.seen_prompts == [guard_mod.SHIELDGEMMA2_PROMPT]

    pipe_yes = FakeGuardPipeline([{"generated_text": "Yes"}])
    clf2 = GuardModelAdapter(family="shieldgemma2", pipeline=pipe_yes)
    assert clf2.classify(make_evidence(png)).nsfw_prob == guard_mod.PROB_UNSAFE


def test_classify_llamaguard_labels(tmp_path, ensure_pil):
    """llamaguard 族:unsafe+S5 → 高分 + 类别码;safe → 低分。"""
    png = write_png(tmp_path / "b.png")
    pipe = FakeGuardPipeline("unsafe\nS5")
    clf = GuardModelAdapter(family="llamaguard", pipeline=pipe)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == guard_mod.PROB_UNSAFE
    assert score.scores["verdict"] == "unsafe"
    assert score.scores["categories"] == ["S5"]
    assert pipe.seen_prompts == [guard_mod.LLAMAGUARD_PROMPT]

    clf_safe = GuardModelAdapter(family="llamaguard",
                                 pipeline=FakeGuardPipeline("safe"))
    assert clf_safe.classify(make_evidence(png)).nsfw_prob == guard_mod.PROB_SAFE


def test_classify_custom_prompt_family(tmp_path, ensure_pil):
    """custom-prompt 族:自定义模板透传,输出按 Yes/No 语法解析。"""
    png = write_png(tmp_path / "c.png")
    pipe = FakeGuardPipeline("Yes")
    clf = GuardModelAdapter(family="custom-prompt", prompt_template="逐字使用这个模板",
                            pipeline=pipe)
    score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == guard_mod.PROB_UNSAFE
    assert pipe.seen_prompts == ["逐字使用这个模板"]


def test_cfg_injection_of_model_path_and_family(tmp_path, monkeypatch):
    """config 注入:cfg.guard_model_path / cfg.guard_family 动态属性生效(getattr 惯例)。"""
    model_dir = tmp_path / "Llama-Guard-4-12B"
    model_dir.mkdir()
    created = install_fake_transformers(monkeypatch)
    cfg = Config()
    cfg.guard_model_path = str(model_dir)   # type: ignore[attr-defined]
    cfg.guard_family = "llamaguard"         # type: ignore[attr-defined]
    clf = GuardModelAdapter(cfg)
    assert clf.model_path == str(model_dir)
    assert clf.family == "llamaguard"
    assert created[0].kwargs.get("model") == str(model_dir)


def test_classify_batch_per_image_in_order(tmp_path, ensure_pil):
    """批量:逐张、保序,与基类默认同语义;每张恰好一次推理。"""
    pngs = [write_png(tmp_path / f"s{i}.png") for i in range(3)]
    probs = iter([guard_mod.PROB_UNSAFE, guard_mod.PROB_SAFE, guard_mod.PROB_UNSAFE])

    class _SeqPipe:
        def __init__(self):
            self.calls = 0

        def __call__(self, image, prompt):
            self.calls += 1
            return "Yes" if next(probs) == guard_mod.PROB_UNSAFE else "No"

    pipe = _SeqPipe()
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=pipe)
    scores = clf.classify_batch([make_evidence(p) for p in pngs])
    assert pipe.calls == 3
    assert [s.nsfw_prob for s in scores] == pytest.approx(
        [guard_mod.PROB_UNSAFE, guard_mod.PROB_SAFE, guard_mod.PROB_UNSAFE])


# ---------------------------------------------------------------------------
# 失败路径:异常/畸形 → 保守 0 分 + error + 遥测(全库惯例)
# ---------------------------------------------------------------------------

def test_pipeline_exception_returns_zero(tmp_path, ensure_pil, caplog):
    png = write_png(tmp_path / "f.png")
    pipe = FakeGuardPipeline(error=RuntimeError("推理崩溃"))
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=pipe)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.guard_adapter"):
        score = clf.classify(make_evidence(png))
    assert score.nsfw_prob == 0.0
    assert "推理崩溃" in score.scores["error"]
    assert pipe.calls == 1
    assert any("守卫模型" in rec.getMessage() for rec in caplog.records)


def test_missing_file_returns_zero_without_inference(tmp_path, ensure_pil, caplog):
    png_missing = tmp_path / "missing.png"
    pipe = FakeGuardPipeline("Yes")
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=pipe)
    with caplog.at_level(logging.WARNING, logger="netsentinel.vision.guard_adapter"):
        score = clf.classify(make_evidence(png_missing))
    assert score.nsfw_prob == 0.0
    assert "error" in score.scores
    assert pipe.calls == 0  # 文件打不开,绝不进入推理
    assert any("守卫模型" in rec.getMessage() for rec in caplog.records)


def test_malformed_output_returns_zero_with_error_marker(tmp_path, ensure_pil, caplog):
    """模型输出无法解析:保守 0 分 + verdict=unparseable + error,计 vision.errors。"""
    png = write_png(tmp_path / "m.png")
    pipe = FakeGuardPipeline("我觉得还行吧")
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=pipe)
    telemetry.reset()
    try:
        with caplog.at_level(logging.WARNING, logger="netsentinel.vision.guard_adapter"):
            score = clf.classify(make_evidence(png))
        assert score.nsfw_prob == 0.0
        assert score.scores["verdict"] == "unparseable"
        assert "无法解析" in score.scores["error"]
        assert telemetry.snapshot()["counters"]["vision.errors"] >= 1
    finally:
        telemetry.reset()


def test_telemetry_records_infer_timer(tmp_path, ensure_pil):
    telemetry.reset()
    try:
        png = write_png(tmp_path / "tel.png")
        clf = GuardModelAdapter(family="llamaguard",
                                pipeline=FakeGuardPipeline("safe"))
        clf.classify(make_evidence(png))
        assert telemetry.snapshot()["timers"]["guard.infer"]["count"] >= 1
    finally:
        telemetry.reset()


# ---------------------------------------------------------------------------
# 确定性:同输入同输出;多线程推理串行(实例锁)
# ---------------------------------------------------------------------------

def test_deterministic_repeated_classify(tmp_path, ensure_pil):
    """确定性:同一输入反复分类,结果逐字节一致。"""
    png = write_png(tmp_path / "det.png")
    clf = GuardModelAdapter(family="llamaguard",
                            pipeline=FakeGuardPipeline("unsafe\nS5"))
    first = clf.classify(make_evidence(png)).as_dict()
    for _ in range(5):
        assert clf.classify(make_evidence(png)).as_dict() == first
    assert parse_yes_no("Yes") == parse_yes_no("Yes") == (guard_mod.PROB_UNSAFE,
                                                          {"verdict": "unsafe"})


class _ThreadProbePipe:
    """用探针锁记录推理并发峰值,验证共享 pipeline 被串行调用。"""

    def __init__(self):
        self.calls = 0
        self.active = 0
        self.peak = 0
        self._probe = threading.Lock()

    def __call__(self, image, prompt):
        with self._probe:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.02)  # 拉长推理窗口:无锁时多线程必然交叠
        with self._probe:
            self.active -= 1
        return "No"


def test_concurrent_classify_serializes_pipeline_calls(tmp_path, ensure_pil):
    """多线程 classify:模型侧要求串行推理 → pipeline 并发峰值必须为 1。"""
    pngs = [write_png(tmp_path / f"t{i}.png") for i in range(4)]
    probe = _ThreadProbePipe()
    clf = GuardModelAdapter(family="shieldgemma2", pipeline=probe)
    results: dict[str, object] = {}

    def worker(path):
        results[str(path)] = clf.classify(make_evidence(path))

    threads = [threading.Thread(target=worker, args=(p,)) for p in pngs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert probe.peak == 1  # 推理全程串行,无并发交叠
    assert probe.calls == 4
    assert len(results) == 4
    assert all(s.nsfw_prob == guard_mod.PROB_SAFE for s in results.values())

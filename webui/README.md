# 净网哨兵 · 人工复核台(webui)

Streamlit Web 复核台:机器初筛 → **人工复核** → 辅助举报流程中的"人工复核"环节。
运营者在浏览器里查看证据、查看 GLM/规则解释,逐条批准或驳回,并可在批准后本地预览
举报计划(playbook)。本页面**绝不自动提交举报**。

## 启动

```bash
# 1) 安装(含 UI 可选依赖)
python -m pip install -e ".[ui]"

# 2) 启动(在项目根目录执行)
streamlit run webui/app.py
```

未安装 streamlit 时直接运行 `python webui/app.py` 会打印中文安装提示并以退出码 1 结束。

数据目录默认为 `./data`;可用环境变量 `NETSENTINEL_DATA_DIR` 覆盖(库、证据、审计、
日志、VLM 缓存会整体迁移到该目录下):

```bash
NETSENTINEL_DATA_DIR=/srv/netsentinel streamlit run webui/app.py   # Windows: set NETSENTINEL_DATA_DIR=...
```

配置读取与 CLI 一致:默认找项目根的 `config.yaml`,不存在则用安全默认值
(`human_gate_required=True` 等红线不可关闭)。

## 功能说明(文字版界面导览)

- **侧栏**
  - 队列概览:待复核 / 已批准 / 已驳回 / 已提交 四项计数(来自 `ReviewQueue.summary()`);
  - 状态筛选下拉框与站点 URL 子串搜索框(大小写不敏感);
  - 数据目录说明:数据根目录、复核队列 SQLite 路径、证据目录与环境变量覆盖方式。
- **主区页签:待复核 / 已批准 / 全部**
  - 条目卡片(折叠展开),标题为 `编号 #id · 域名 · 判定中文`,徽章颜色:
    红色=高置信(nsfw)、黄色=疑似(suspect)、绿色=未发现(clean);
  - 概要指标:综合分值 agg、机器判定、当前状态;站点、入列时间、证据包 zip 路径;
  - 证据图片网格:从报告 `pages` 的图片与整页截图收集本地文件,自动过滤不存在的
    路径与超过 8MB 的文件,每行 3 列、每卡片最多展示 9 张;
  - 模型/规则解释要点:从 `intel` 的链接特征 / 文本特征 / 页面级 VLM / 融合(fusion)
    提炼的中文要点,最多 6 条;`intel` 原始 JSON 折叠展示;
  - **人工拍板区(仅待复核条目)**:
    - 批准:必须先勾选"我已人工核实证据真实有效"双重确认,按钮才可用
      (调用 `ReviewQueue.approve`,进入举报队列);
    - 驳回:填写驳回理由后调用 `ReviewQueue.reject`,备注留档;
  - **举报计划预览(已批准条目)**:选择门户 12377(中央网信办举报中心)或
    扫黄打非,本地生成 `plan_12377` / `plan_shdf` 计划并以 playbook markdown 展示。
    计划文本包含 `HUMAN_GATE` 人工门步骤——验证码输入与最终提交必须人工完成。

## 安全提示

- **仅限本机使用**:该页面没有任何鉴权,不要绑定到非回环地址或暴露到公网;
  默认服务地址遵循 `service_host=127.0.0.1` 的安全基线。
- **批准操作不可逆**:pending → approved 后无法回到 pending(状态机限制),
  批准前务必逐张核对证据图片;驳回同样不可撤销。
- **不自动提交**:本页面只做复核与计划"预览",不会打开浏览器、不会访问
  12377 / 扫黄打非站点、不会代填验证码;真实提交须另行走 CLI/服务端流程,
  并始终通过 HUMAN_GATE 人工门。
- **举报须真实**:虚假举报违法;批准即代表你已人工核实证据真实有效。

## 测试

```bash
python -m pytest tests/test_webui_smoke.py -q
```

纯逻辑函数(卡片组装、过滤、证据路径筛选、判定中文映射、数据目录覆盖)不依赖
streamlit,可在未安装 UI 依赖的环境下运行;UI 部分用 `pytest.importorskip("streamlit")`
保护,缺依赖时自动跳过。

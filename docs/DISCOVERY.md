# 线索发现指南(优先 Yandex · 自定义关键词)

> 定位:把运营者的**调查关键词**变成"待筛查线索清单";发现 ≠ 判定 ≠ 举报。

## 红线(27/28)

1. **发现即线索**:搜索引擎返回的每个 URL 都只是待筛查线索,必须走完整
   扫描 → 判定 → 人工复核 → 逐组声明 → 逐条人工门举报 流程;发现本身不构成任何处置依据。
2. **关键词零内置**:本模块不内置任何敏感搜索词,所有关键词必须由运营者显式提供;
   `discovery_online` 默认 `false`(零外呼),查询间隔 ≥1s、单查询 ≤50 条、单轮 ≤500 条由配置强制。

## 快速开始

```
# 1)准备关键词文件(每行一词;或 .yaml 列表)——词由运营者自拟
#    keywords.txt 示例:
#    某线索词A
#    某线索词B site:cn

# 2)离线自检(零外呼):确认装载与配置
python -m netsentinel.discovery --keywords-file keywords.txt --offline-check

# 3)发现(默认引擎 yandex;需 discovery_online: true)
python -m netsentinel.discovery --keywords-file keywords.txt --out leads.txt

# 4)进入 V6 批量流水线
python -m netsentinel.batchflow --input leads.txt
```

## Yandex(优先引擎)

- 使用 **Yandex Search XML 官方 API**(付费接口,凭 `user` + `key` 鉴权);凭据获取以官方控制台为准;
- 配置三选一:
  ```yaml
  discovery_online: true
  yandex_xml_user: "你的user"
  yandex_xml_key:  "你的key"
  ```
  或环境变量 `NETSENTINEL_YANDEX_USER` / `NETSENTINEL_YANDEX_KEY`(推荐,避免入库);
- 缺凭据或缺开关时构造/查询即拒绝并给中文指引,不会盲发请求;
- 引擎返回的官方 `<error>` 会原样转述(如配额/限流错误码);
- **不解析、不绕过任何验证码与反爬页面**;接口用法以官方文档为准,端点可经子类覆盖。

## 备选引擎

- **SearXNG**(自托管,无第三方 ToS 顾虑):`discovery_engine: searxng` +
  `searxng_base_url: http://127.0.0.1:8888`(实例需开启 `format=json`);
- **mock**(离线测试):`--engine mock`,确定性生成结果,用于管线演练。

## 关键词与模板

- 来源:`--keywords 词1,词2` 内联 / `--keywords-file k.txt`(每行一词,`#` 注释)/ `.yaml`(`[词...]` 或 `keywords: [...]`);
- 模板(可选,运营者自拟):`--template "{kw} site:cn" --template "报告 {kw}"`,支持 `{kw}` 占位符;默认原样查询;
- 去空、去重、保序;空词表直接报错(零内置)。

## 行为细节

| 项 | 默认 | 说明 |
| --- | --- | --- |
| discovery_query_delay_s | 3.0 | 相邻查询最小间隔(下限 1s,引擎礼貌限速) |
| discovery_max_per_query | 10 | 单查询最多取结果数(1~50) |
| discovery_max_total | 100 | 单轮线索总预算(1~500) |
| discovery_cache_db / ttl | data/discovery_cache.db / 168h | 同查询结果缓存,命中零外呼 |
| 排除域 | yandex/google/bing/github 等 | 引擎自身与基础设施域自动剔除,可经 `discover(exclude_domains=...)` 追加 |
| 去重 | canonical 可注册域 | 同站多结果只留首条(镜像交给 V6 归组处理) |

## 输出

`leads.txt` 带用途头注释(“仅待筛查线索,非判定结论”),与 `bulk_intake.load_bulk` 完全兼容;
`python -m netsentinel.batchflow --input leads.txt` 即进入 扫描 → 归组 → 声明 → 批量举报(干跑默认)流水线。

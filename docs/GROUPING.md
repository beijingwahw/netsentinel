# NetSentinel 归纳规则白皮书(GROUPING)

> 适用版本:V6(A103–A118 等)。本文说明批量线索如何按"同一名称"归纳为案件组:canonical 可注册域归一、镜像变体、团伙证据链与阈值调优、案件命名、误并/漏并排查、grouping_bench 指标读法。批量操作流程见 `docs/BATCH_GUIDE.md`;行为以 `CONTRACTS-V6.md` 与实际代码为准。

---

## 1. 为什么需要"归纳同名"

V6 之前是"单站办案":一条线索一个站、一次扫描一份证据。真实违法站点却常常是**一个团伙多面出口**——同一内容挂在 `www.a.com`、`img.a.com`、`a.com:8443/m`,甚至换域名做镜像(`b-example.co.uk`)。逐条举报这些变体既浪费每日额度,也割裂了证据。

V6 的归纳目标:**同一名称的全部线索归并为一个"案件组"——一份合并证据包、一次举报**。归纳不改变安全语义:组只是编排单位,入列、复核、声明、逐条人工门一样不少(红线 24/25,见 BATCH_GUIDE.md 第 0 节)。

## 2. 核心概念:基础组与案件组

```
URL 线索
  │ canonical_key 相同(可注册域相同)          ← 第一级:同站
  ▼
基础组(CaseGroup,A104 case_group.py)
  │ 团伙证据链:图谱关联边 / 图片指纹重叠       ← 第三级:团伙
  ▼(union-find,A105 group_linker.py)
案件组(仍是 CaseGroup,同构)
  = 一份合并证据包(A109 merge_bundles)= 一次举报
```

- **基础组**:同 canonical_key 的所有条目归并;www/子域/端口/路径差异视为同站变体(host 记入 `aliases`)。
- **案件组**:基础组之间若存在团伙证据(检索图谱关联边,或图片指纹集合重叠率达阈值),经 union-find 并入同组;`CaseGroup` 字段:`name`(canonical 主名)、`aliases`(全部域名)、`entry_ids`(成员条目)、`site_urls`、`agg_max`(组内最大)、`verdict`(最严重档)、`image_sha_set`(图片 sha 并集)、`created_at`。
- 聚合口径:`agg_max` 取组内各报告 `agg_nsw_prob` 最大值;`verdict` 取最严重档(clean < suspect < nsfw);`image_sha_set` 取页面图片 sha256 并集去重;输出按 (verdict 档, agg_max, 组规模) 降序,末位按组名升序,结果恒确定。

## 3. "同一名称"的三级判定

| 级 | 判定 | 依据模块 | 结论 |
| --- | --- | --- | --- |
| 第一级 | **canonical 可注册域相同** → 同站 | A103 `canonical.py`(纯本地,零网络) | 并入同一基础组 |
| 第二级 | **镜像变体**:www / 子域 / 端口 / 路径 / scheme 差异 | A103 `alias_label` | 不拆组,差异记为组内变体描述(aliases) |
| 第三级 | **团伙证据链**:图谱关联边(shared_image / phash_near / redirect / shared_template)或图片指纹集合重叠率 ≥ `group_merge_phash_overlap` | A46 `graph.py` + A105 `group_linker.py` | 基础组之间并入同一案件组 |

三级是递进关系:先做最强的同站归并(确定性),再做弱证据的团伙归并(概率性、可调参)。解析失败的 URL(键为空)**各自独立成组**(以原始 URL 为组键),互不相干的非法 URL 不会被错误并组;`is_same_site` 任一侧解析失败即返回 False——两个"解析不了"的输入不能因为同为空串而判为同站。

## 4. canonical 算法说明(A103)

`canonical_key(url)` 把任意 URL 归一为**可注册域**(registrable domain)小写键,处理流水:

1. 解析 host:小写、去端口/认证信息(user:pass@)/IPv6 方括号/尾部圆点及前后空白;
2. host 是 IP 字面量(v4/v6,`ipaddress` 校验)→ 返回规范化 IP 串(v4 原样;v6 压缩、小写);
3. 单标签主机(localhost 等内网名)→ 原样返回;
4. 其余逐级去掉最左标签,直到剩余部分 ∈ `MULTI_SUFFIXES` ∪ 通用 TLD(≥2 位纯 ASCII 字母),再向左补一个标签即为可注册域(首个命中即最长后缀,PSL 语义);
5. 解析失败 / 无 host → `""`;尾标签不是合法 TLD 的畸形域名(数字尾/单字母/非 ASCII)→ 整串兜底。

`MULTI_SUFFIXES` 固定表(12 项,契约给定,禁增删):`com.cn / net.cn / org.cn / gov.cn / edu.cn / ac.cn / com.hk / com.tw / com.sg / co.jp / co.uk / com.au`。此类后缀本身不可单独注册,可注册域需再向左取一个标签。

端口/查询/片段/scheme/路径差异**不影响**结果;函数纯本地、决定、恒小写、不发网。`canonical_name(url)` = canonical_key,解析失败时回退为原 URL 截断(80 字),保证任何输入都有非空可展示名称。

### 4.1 示例表(以下输出均为实测)

| 输入 URL | canonical_key | 说明 |
| --- | --- | --- |
| `http://a.b.example.com.cn:8080/x?y` | `example.com.cn` | 命中多段后缀 com.cn,去掉子域 a.b;端口/路径/查询忽略 |
| `https://www.example.com/` | `example.com` | www 前缀归并 |
| `https://shop.example.co.uk/cart` | `example.co.uk` | 命中多段后缀 co.uk,取末三段 |
| `https://example.com/a/b?x=1` | `example.com` | 通用 TLD com,路径/查询不影响 |
| `https://Example.COM.` | `example.com` | 大小写与尾点归一 |
| `http://192.168.10.7:8000/a` | `192.168.10.7` | IPv4 直连 → IP 串(端口/路径忽略) |
| `http://[2001:0DB8::0001]/x` | `2001:db8::1` | IPv6 压缩小写 |
| `http://localhost:8080/app` | `localhost` | 单标签主机原样(端口/路径忽略) |
| `http://x.y.999/` | `x.y.999` | 尾标签 999 非合法 TLD → 整串兜底(host 结构完整,不算解析失败) |
| `"not a url"` / 空串 / 无 host | `""`(空) | 解析失败 → 空键(条目各自独立成组) |

### 4.2 镜像变体描述(`alias_label`)

组内每个 URL 相对 canonical 的差异由 `alias_label` 记为中文变体描述(片段用 `/` 拼接),用于 aliases 展示与举报文本:

| URL | alias_label |
| --- | --- |
| `https://example.com/` | `主站` |
| `https://www.example.com/` | `www 前缀` |
| `https://example.com/a` | `带路径` |
| `http://a.b.example.com.cn:8080/x` | `子域 a.b/端口 8080/带路径` |
| `http://192.168.10.7:8000/a` | `端口 8000/带路径` |

片段构成:www 前缀(最左标签恰为 www)/ 子域 x(可注册域之上的其余前缀)/ 端口 N(显式写的端口,含显式 80/443)/ 带路径(路径非空且不是 `/`;查询/片段不计)。IP 直连与单标签主机没有子域概念,只可能出现端口/路径片段;解析失败返回 `""`。

## 5. 合并依据四种与阈值

基础组/案件组之间按下表四种依据判定合并(前一种恒生效,后三种属团伙归并、可调):

| # | 依据 | 判定内容 | 证据来源 | 指向强度 | 配置开关 |
| --- | --- | --- | --- | --- | --- |
| 1 | 同站(canonical) | `canonical_key` 相同(www/子域/端口/路径差异全部抹平) | A103 纯本地计算 | 最强(必然同站) | 无(恒生效) |
| 2 | 图片指纹重叠 | 两组 `image_sha_set` 的 Jaccard 相似度 ≥ `group_merge_phash_overlap` **且交集非空**(零重叠永不并组) | A104 收集的页面图片 sha256 | 强(共享原图) | `group_merge_phash_overlap`(默认 0.3) |
| 3 | 共享图 / 感知哈希 / 重定向 | 证据图谱 `related_sites` 命中对方组成员,via 含 `shared_image` / `phash_near` / `redirect` 任一 | A46 检索图谱 | 强~中 | 恒计入 |
| 4 | 共享模板 | via 仅含 `shared_template`(建站模板复用) | A46 检索图谱 | 弱(指向性弱,允许关闭) | `group_merge_template`(默认 True) |

判定实现(`group_linker.merge_groups`):图谱边(依据 3/4)与指纹边(依据 2)合并后经 **union-find**(路径压缩 + 按秩合并)聚连通分量;每个分量合成一个案件组。`graph=None` 或图谱查不到关联时,退化为仅凭指纹重叠并组。

合并规则(分量内多组 → 一个案件组):

- `name` 取**规模最大子组**的主名(并列取站点数多者,再按名称升序,保证确定);
- `aliases` / `entry_ids` / `site_urls` / `image_sha_set` 取并集去重;
- `agg_max` 取最大;`verdict` 取最严重档;`created_at` 取最早(案件组溯源到首个子组建立时刻);
- 未并组的成员**原对象透传**;幂等:对输出重跑,已并组的成员不再分裂、字段不再变化。

归组的落点:批量流程中 `ops/batch_scan.group_and_enqueue` 以 `cfg.group_merge_phash_overlap` / `cfg.group_merge_template` 调用上述两级归并——**调参后重跑归组即可生效,无需改代码**。

## 6. 阈值调优:`group_merge_phash_overlap`

重叠率按 **Jaccard 相似度**计算:`|A∩B| / |A∪B|`(两组图片 sha 集合的交集/并集;双空集为 0)。判定为 `>=` 阈值且交集非空;实现为整数计数相除,无浮点累积误差,边界值(如 3/10 对 0.3)严格成立,不需要容差。

| 调整方向 | 影响 | 适用场景 |
| --- | --- | --- |
| 调大(0.3 → 0.5) | 只有共享图比例很高的组才并:组更碎、误并减少、漏并增多 | 误并多(不同团伙共用 CDN 素材/通用图库被错并) |
| 调小(0.3 → 0.2 / 0.15) | 少量共享图即并:组更大、漏并减少、误并增多 | 漏并多(镜像站只挂了少量相同图片) |

**建议起点 0.3**(契约默认):即两组每 10 张图有 3 张相同即视为同伙,兼顾"共享主视觉/横幅"这类典型镜像特征,又不至于把只共用一两张通用素材的站点拉进来。调参配套手段:把 `group_merge_template` 设为 `false` 可单独排除弱指向的模板证据(见第 8 节误并排查)。注意:两组图片**完全**不相交时,任何阈值都不生效——漏并的兜底是图谱边(依据 3),不是继续降阈值。

## 7. 案件命名规则(A116)

| 场景 | 名称(展示) |
| --- | --- |
| 单站组 | `example.com`(主域名) |
| 多站组(基础算法) | `example.com(含 5 个关联站点)`(n>1 时附站点数) |
| 增强版(GLM client 注入) | 在基础名上附加依据 intel 概括的 ≤20 字特征短语;模型不可用时离线回退到基础名 |

- `CaseGroup.name` 本身为 canonical 主名;团伙合并后的组名取**规模最大子组**的主名(见第 5 节),保证大案不因并组被小站改名。
- 展示行:`group_title_row(group)` 提供组名/站点数等字段供 TUI、WebUI 表格渲染。
- 举报文本(A106 `dedup_report_url`):生成"主站 + 镜像清单"——主 URL 加全部别名域列表,≤200 字,随举报一并提交,接收方可见全部变体出口。

## 8. 误并排查(想拆组)与漏并排查(想并组)

### 8.1 误并:两个不相干的站点进了同一组

按证据强度从弱到强排查:

1. **查图谱边种类(最常见)**:用 `related_sites` 查看两组之间靠什么边连上——
   ```python
   from netsentinel.intel.graph import EvidenceGraph
   g = EvidenceGraph("data/graph.db")
   for item in g.related_sites("https://www.example.com/"):
       print(item["site"], item["via"], item["weight"])
   ```
   若 `via` 仅含 `shared_template`(四种边:`shared_image` / `phash_near` / `redirect` / `shared_template`),说明只是建站模板复用——配置 `group_merge_template: false` 即可排除此类弱证据并组;
2. **查指纹重叠**:若两组图片 sha 交集不小但确属巧合(共用图库/CDN 素材),**提高** `group_merge_phash_overlap`(如 0.3 → 0.5)收紧并组门槛;
3. **善后**:调整后重跑归组;已入列的错误组在复核中 `reject` 处理(rejected 条目不参与批量)。

### 8.2 漏并:明显同伙的站点分成了多组

1. **降阈值**:`group_merge_phash_overlap` 0.3 → 0.2(镜像只共享少量同图时重叠率不足);
2. **查图谱**:两组图片完全不同时,指纹路径走不通,确认扫描阶段是否建出了 `shared_image` / `phash_near` / `redirect` 边(无图集则无共享图边,无跳转则无 redirect 边);`group_merge_template: false` 时模板证据也被忽略;
3. **查 canonical**:站点 URL 解析失败(键为空)会各自独立成组——先用第 4.1 节示例表核对两站的 canonical_key 是否都非空且可归并(注意:不同可注册域永远不会因 canonical 并组,这是设计而非缺陷);
4. **看统计**:A117 `group_stats.stats()` 的"单站组占比"与"最大组 Top5"可辅助发现规模化漏并。

两个方向的调整都只影响"第三级团伙归并";第一级同站归并是确定性规则,不随阈值变化。

## 9. grouping_bench 指标读法(A118)

`benchmarks/grouping_bench.py` 用合成语料度量分组质量:

- `make_synthetic(n_sites=12, mirrors_per=2)`:合成主域 + 镜像 + 同团伙共享图暗示的 `(url, label)` 语料(标签由生成器自带);
- `evaluate(grouping, labels) -> {"purity": float, "completeness": float}`:把分组结果与真实标签比对;
- `run(out_dir)`:输出中文 `report.md` / `report.json`,可 CLI 运行(旗标以 `--help` 为准)。

指标怎么读:

| 指标 | 衡量什么 | 偏低说明 | 对应排查 |
| --- | --- | --- | --- |
| `purity`(纯度) | 每个组内成员是否同属一个真实团伙(组内有没有混入不相干站点) | 低 = **误并**方向的问题 | 第 8.1 节:关模板并组 / 提高阈值 |
| `completeness`(完整度) | 同一真实团伙的站点是否被完整收进同一组(有没有被拆散) | 低 = **漏并**方向的问题 | 第 8.2 节:降低阈值 / 补图谱边 |

基准读数:**完美分组时 purity = completeness = 1.0**(契约测试锚点);"镜像全归并"对应 completeness 高,"跨站误并检测"对应 purity 不被拉低。典型组合:purity 低而 completeness 高 = 过度合并(阈值过低或模板证据误伤);purity 高而 completeness 低 = 过度拆分(阈值过高或图谱缺边)。改配置后在同一语料上重跑 `evaluate`,两个数的变化即调参效果——这正是阈值调优(第 6 节)的量化依据。

## 10. 相关模块索引

| 模块 | 职责 |
| --- | --- |
| `netsentinel/intel/canonical.py`(A103) | 可注册域归一:canonical_key / canonical_name / is_same_site / alias_label |
| `netsentinel/intel/case_group.py`(A104) | 同站归并为基础组(CaseGroup / group_entries) |
| `netsentinel/intel/group_linker.py`(A105) | 团伙归并(merge_groups,union-find) |
| `netsentinel/intel/graph.py`(A46) | 证据图谱与 related_sites 查询(via 边种类) |
| `netsentinel/decision/dedup_policy.py`(A106) | 合并规则封装 DedupRules + 举报用主站/镜像清单文本 |
| `netsentinel/intel/name_suggest.py`(A116) | 案件命名(suggest_name / 增强版 / group_title_row) |
| `netsentinel/intel/group_stats.py`(A117) | 分组统计 / CSV 导出 / repeat_offenders |
| `benchmarks/grouping_bench.py`(A118) | purity / completeness 基准 |
| `netsentinel/ops/batch_scan.py`(A108) | 归组在批量流水线中的调用点(group_and_enqueue) |

---

*维护:A121(文档工程)。规则口径变更请先改 `CONTRACTS-V6.md`,再同步本文。*

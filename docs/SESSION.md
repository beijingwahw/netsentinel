# 会话累积流程(SESSION)

把"跨会话累积 → 存档 → 续跑 → 确认举报"串成一条命令:扫描结果**自动按
网站名字(可注册域)分类累积**,用户结束运行时**自动存档**,下次运行
**在上一次基础上继续**;用户确认举报时,**对全部累积结果按网站名字逐组举报**。

新增模块:

- `netsentinel/sessionstore.py` —— 会话累积库(SQLite,`<data_dir>/session/accumulation.db`):
  同 canonical 域一组(www/子域/端口变体都算同站),verdict 取历史最严重档、
  agg 取历史最大、扫描明细 append-only(`scan_events`);存档写
  `<data_dir>/session/archive.json`(原子替换);
- `netsentinel/sessionflow.py` —— 总流程 CLI(下述五模式)。

## 命令

```bash
# 1) 扫描 + 累积 + 自动存档(清单或单站;永不提交)
python -m netsentinel.sessionflow --input leads.txt --config config.yaml
python -m netsentinel.sessionflow --url "https://待核查站点.example/"

# 2) 查看累积现状(全部网站组:判定/聚合分/扫描次数/待举报|已举报)
python -m netsentinel.sessionflow --status

# 3) 立即存档(结束运行时手动兜底;每次扫描/举报结束也会自动存档)
python -m netsentinel.sessionflow --archive

# 4) 确认举报:列出全部待举报组 → 人工确认 → 按网站名字逐组举报
python -m netsentinel.sessionflow --report --reviewer 张三           # 默认干跑
python -m netsentinel.sessionflow --report --reviewer 张三 --exec  # 真实提交
```

## 行为细节

- **累积口径**:组名 = canonical 可注册域(与 V6 案件分组、复核队列 note 的
  `[组:组名]` 标记完全同口径),因此会话组能直接对上复核队列条目;同站
  重复扫描只递增 `scan_count` / 收紧最严重档,不重复建组;
- **续跑**:累积库即延续基座,重开同一库自动继续;`--status` 首行的
  "延续基线"显示上一次的组数与扫描次数;
- **确认举报**(`--report`)流程:

  1. 取累积库全部**待举报组**(按 严重档 → agg 降序),扫复核队列展示
     各组条目状态(待审/已批);条目已全部终态的组自动收档;
  2. **人工确认**(确认前零状态变更):审核人留名 + 显式 `y`(提示语含
     "我已逐组人工核实");非 `y` 一律中止,什么都不改;
  3. 确认动作 = 批准各组 pending 条目(留审核人署名)+ 逐组写批量声明
     (`BatchReview.attest`,红线 25 留痕);
  4. 按网站名字排序组装条目,超过 `batch_max_items` 自动分批,每批经
     `BatchState` 建批 + `run_batch` 顺序举报(**恒 `auto_confirm=False`**,
     每条仍走 HUMAN_GATE 人工门,红线 24;频控/每日额度不放宽,红线 26);
  5. 真实提交成功的条目回写复核队列(`approved → submitted`,与单站
     `submit` 同口径,防止下次重复收集);全组完成的组标记"已举报"并
     刷新存档;频控挂起的部分下次 `--report` 自动续跑。

## 红线对照

| 红线 | 落实方式 |
| --- | --- |
| 2(提交前必须人工确认) | 确认前零状态变更;批准/声明只发生在显式 y 之后 |
| 24(批量逐条人工门) | 对执行器只透传;`auto_confirm` 恒 False(源码级断言测试锁定) |
| 25(批量确认声明留痕) | 每组 `attest`(审核人 + 含"人工核实"文本,sqlite+审计) |
| 26(频控不得放宽) | 频控由 `run_batch` 收口;超上限自动分批,不绕过 |
| 10/15(测试零外呼) | 新增测试全离线注入(22 项),不访问网络与真实门户 |

## 数据位置

| 文件 | 说明 |
| --- | --- |
| `<data_dir>/session/accumulation.db` | 累积库(组表 + 扫描明细,跨会话延续) |
| `<data_dir>/session/archive.json` | 最近一次存档快照(人类可读) |

存档快照结构:`{"archived_at", "stats": {groups/pending/reported/scans},
"pending_groups": [...], "reported_groups": [...]}`(每组含 name/aliases/
scan_count/verdict/agg_max/first_seen/last_seen 等,不含报告正文)。

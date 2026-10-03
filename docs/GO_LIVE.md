# 真实环境接入指南(Go-Live)

> 红线 38:**真实接入 ≠ 自主运行**。本系统接入真实环境后,密钥、目标清单、逐组声明、
> 每条举报的验证码与最终提交**永远属于运营者**。验证器只做只读检查,绝不产生举报。

## 一、一步体检

```
python -m netsentinel.golive check          # 离线体检(配置/本地服务/浏览器/依赖/存储)
python -m netsentinel.golive check --net    # 追加真实外网只读探活(GET 首页取样 512B)
python -m netsentinel.golive prepare        # 生成 config.production.yaml 模板
```

状态语义:✅ ok=就绪;⏳ gated=待运营者提供(密钥/服务);❌ fail=阻塞;➖ skip=可选缺失。
退出码:无 fail → 0;有 fail → 2。

## 二、2026-10-02 本机真实实测结果

| 项 | 结果 |
| --- | --- |
| 通用外网出口(pypi) | ✅ HTTP 200 |
| 智谱 GLM API | ✅ HTTP 401(端点可达,需鉴权) |
| Yandex XML 端点 | ✅ HTTP 403(端点可达,需鉴权) |
| **www.12377.cn(只读)** | ✅ HTTP 200 |
| **www.shdf.gov.cn(只读)** | ✅ HTTP 521(防护层预期内,与 V2 调研一致) |
| OpenAI API | ❌ 本网络不可达(已知墙内限制;可换 glm/qwen/gemini) |
| chromium 真实执行器 | ✅ 可启动 |
| GLM 密钥 | ✅ 密钥环已配置(向导通道) |
| **真实视觉分类(端到端)** | ✅ **glm-5.3-flash 云端评分成功**:测试图 nsfw_prob=0.02、"正常"、中文依据、5.2s |

## 三、接入真实环境的操作顺序

1. `golive check --net` 确认基础设施(上述全绿或仅可选缺失);
2. `golive prepare` 生成生产模板,逐项确认四个真实开关
   (`allow_network / vlm_online / discovery_online / dry_run_default=false`);
3. 视觉模型:本地(`ollama pull llava` → 自动接管)或云端(向导页/`modelmgr` 配密钥);
   `python -m netsentinel.vlmctl ping glm` 人工显式核验模型;
4. 线索与目标:`python -m netsentinel.discovery --keywords-file 词表.txt --engine yandex`
   (或自备清单)——**产出仅为线索,人工核实后**写入 urls.txt;
5. 扫描与收官:`python -m netsentinel.finishflow --input urls.txt --tier high`;
6. 人工环节(不可省略):复核队列 approve → `batch_tui` 逐组声明(Y)→
   `finishflow --report --resume <批次> --exec`:**每条停在 HUMAN_GATE,由你输入验证码并确认**。

## 四、已知真实环境注意事项

- **glm-5.3 系思考模型**:`response_format=json_object` 会导致输出在最后一个花括号处截断
  (2026-10 实测);两个传输层已修复(该族省略 response_format,提示词本就限定 JSON)。
- 扫黄打非主站 521 防护:实际表单在 bzpt.shdf.gov.cn(V2 调研),上线前须人工核验入口;
- 门户选择器以契约 §5 为默认,上线前按 `docs/portal_*_notes.md` 清单人工核验一次;
- 提交频控(60s/次、每日 5 条)在批量与高档并发下**均不放宽**(红线 26/35)。

## 五、永远属于运营者的环节(红线 38)

密钥保管 · 目标清单授权 · 逐组人工核实声明 · 每条验证码与最终提交 · 举报内容真实性的法律责任。

# benchmarks/corpus —— 离线基准标注语料(NetSentinel A37)

> **合成语料,仅用于管线基准,不含真实违规内容。**

- 全部图片由 `scripts/make_png.py`(纯标准库 PNG 生成器)程序化生成的**纯色 PNG**,
  文件名中的关键词仅供桩分类器(stub)按规则打分,与真实图像内容无关;
- 构成(尺寸为 200x200 与 400x300 混合,均不低于 `Config.min_image_px=200`):
  - `nsfw_hi_*.png` × 12,标注 `nsfw`(stub 规则分 0.97);
  - `nsfw_mid_*.png` × 6,标注 `borderline`(stub 规则分 0.72);
  - `normal_*.png` × 12,标注 `clean`(stub 规则分 0.02);
- `labels.json`:`{文件名: 标签}`,标签取值 `nsfw | borderline | clean`
  (读取端也兼容 `[{"file": ..., "label": ...}]` 数组格式);
- 重建:`python benchmarks/run_benchmark.py --make-corpus`(确定性生成,字节级可复现);
- 用途:验证"评分 → 阈值 → 指标"链路、演示阈值选型。stub 分数由文件名决定,
  任何指标均为**管线锚点**,不代表真实识别能力;生产请使用 glm / nudenet 集成。

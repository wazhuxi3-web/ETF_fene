# 深交所 ETF 份额采集设计

## 目标

在现有上交所 ETF 份额采集工具中增加深交所采集能力。两家交易所数据写入同一张 `ETF` 表，`total_share` 统一保存真实份额，深交所页面返回的“万份”乘以 10000 后入库。

## 数据来源

深交所官方页面 `https://www.szse.cn/market/fund/volume/etf/index.html` 使用接口：

`https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=scsj_fund_jjgm&jjlb=ETF&txtStart=YYYY-MM-DD&txtEnd=YYYY-MM-DD`

接口返回 `size_date`、`fund_code`、`security_short_name`、`current_size`。`current_size` 的单位是“万份”，接口支持的查询区间不超过 6 个月。数据说明以 T+1 日早间更新的 T 日规模为准。

## 存储

`ETF` 表增加 `exchange` 和 `share_unit` 字段：

- `exchange`: `SSE` 或 `SZSE`
- `share_unit`: 统一为 `share`
- `source`: 保留具体接口来源

唯一性从“日期 + 代码”升级为“日期 + 交易所 + 代码”，差额按交易所和代码分别计算。旧表中的既有记录按 `SSE`、`share` 迁移默认值。

## GUI

采集区域增加交易所选项：上交所、深交所、沪深两市。深交所日期区间自动按 6 个月切片后请求，沿用现有多线程、连通性测试、失败暂停和手动继续机制。

## 错误处理

空日期、接口错误、响应 JSON 错误和无数据都转为现有的网络/采集错误路径；不会自动跳过失败日期。重复采集通过新的唯一键更新，不新增重复行。

## 验证

增加深交所响应解析、万份换算、日期参数、相同代码跨交易所共存和重复更新测试；然后运行全量单元测试和 Python 编译检查。

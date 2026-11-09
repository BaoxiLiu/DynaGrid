# DynaGrid 开发约定

- 默认中文简短回复；先说明结果。新代码、注释、日志以中文为主。
- 主线为paper_grid纸面引擎；入口python -m paper_grid。仅公开行情，无实盘下单接口。
- policy只做决策；engine维护订单与账本；run与replay共用Engine.on_packet，不复制策略或撮合逻辑。
- 配置集中于Settings；状态写入使用storage.write_json。配置变更显式迁移，保留仓位、余额和历史额度。
- 测试：python -m unittest discover -s tests -q；演示：python -m paper_grid backtest --demo。
- 数据、旧代码和私有部署记录在被忽略的data目录。绝不读取、打印、上传.env或密钥，不发布个人路径、服务器地址、账本。
- 保持文件少、入口简单；不要新增无必要的框架、抽象层或重复版本。

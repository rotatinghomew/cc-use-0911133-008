# shelter_route_capacity

安置资源资料处理：路网状态、床位容量、家庭安排、溢出和入住核对。当前入口是 `shelter_route_capacity.allocation.cli`，它使用本地 JSON 资料和 SQLite 状态；目录 `shelter_route_capacity` 存放业务模块，`tests/shelter_allocations` 验证既有业务行为。

另有一个独立的离线分析入口 `shelter_route_capacity.access`：读取批量分析资料 `fixtures/access_scenarios.json`，把纯路网可达上界与容量竞争下的安置结果分开，逐方案给出净增服务人数、受益转移、最弱覆盖群体（含最优解范围）和不可行原因。它只做方案比较，不输出最近距离表，不接管现场调度，也不影响上面的单笔容量预留流程。

## 环境

使用 Python 3.11，全部依赖来自标准库。`requirements.txt` 为空，不需要下载数据、模型或第三方包。数据库在运行时创建，可以删除临时数据库重新开始。

## 检查

```bash
python3 -m unittest discover -s tests/shelter_allocations -v
python3 -m unittest discover -s tests/analysis_context -v
python3 -m compileall -q shelter_route_capacity tests scripts
```

## 运行

```bash
python3 -m shelter_route_capacity.allocation.cli  fixtures/demo.json --database /tmp/shelter_route_capacity-demo.sqlite3
python3 -m shelter_route_capacity.access fixtures/access_scenarios.json --output /tmp/access-scenarios
python3 scripts/check_project.py
```

`check_project.py` 串行执行测试、编译和真实业务入口，将本次业务输出完整打印到终端；状态文件使用独立临时目录，重复运行不沿用上次状态。

批量分析资料位于 `fixtures/access_scenarios.json`，字段含义见 `docs/data.md`。它与当前单笔流程的输入格式不同，不能直接交给原来的入口处理。`shelter_route_capacity.access` 的分析假设（家庭不可拆、必要设施为硬约束、床位多村共享、封闭区间左闭右开、同等最优解的规范选取与范围）写在输出报告的方法说明一节；`tests/analysis_context` 用人工可枚举的小例子校验资源守恒、硬约束与多解，并回归批量资料上手工核算的数值。

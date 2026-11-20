# 深度登记 考勤跨夜班与加班分段核算

规模：中型。核心规则：跨夜按班次归属日配对，休息区间先扣除，再按法定日期和工时阈值切分倍率。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 打卡采信 | 查看并复算打卡采信 | `/api/m1_ingest` | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py` |
| M2 班次日历 | 查看并复算班次日历 | `/api/m2_mapping` | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py` |
| M3 跨夜配对 | 查看并复算跨夜配对 | `/api/m3_versions` | `app/modules/m3_versions/rules.py` | `tests/test_versions.py` |
| M4 缺卡复核 | 查看并复算缺卡复核 | `/api/m4_segments` | `app/modules/m4_segments/rules.py` | `tests/test_segments.py` |
| M5 工时切段 | 查看并复算工时切段 | `/api/m5_calculate` | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py` |
| M6 薪资对账 | 查看并复算薪资对账 | `/api/m6_reconcile` | `app/modules/m6_reconcile/compare.py` | `tests/test_reconcile.py` |
| M7 周期归档 | 查看并复算周期归档 | `/api/m7_archive` | `app/modules/m7_archive/export_out.py` | `tests/test_archive.py` |
| M8 计薪批次 | 裁决在途班次、重放与封存 | `/api/m8_batch` | `app/payroll/batch.py` | `tests/test_batch.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | GB18030中文 | 固定UTF-8失败 | 按编码链回退 | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_legacy_encoding` |
| TRAP-02 | 表头BOM | 首列键失配 | 去除BOM | `app/modules/m1_ingest/decode.py` | `tests/test_ingest.py::test_bom` |
| TRAP-03 | 引号内换行 | 分行误计 | csv逻辑记录 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_preserves_multiline_note` |
| TRAP-05 | 列数错位 | 静默补列 | WIDTH隔离 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_width_isolation` |
| TRAP-06 | 全角千分位 | float失败 | NFKC与Decimal | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_thousands` |
| TRAP-07 | 空值哨兵 | 空值当零 | MISSING隔离 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_missing_numeric` |
| TRAP-08 | 前导零ID | 编码失真 | 字符串保形 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_identifier_zeroes` |
| TRAP-10 | 浮点误差 | 累计失准 | Decimal求和 | `app/modules/m2_mapping/coerce.py` | `tests/test_mapping.py::test_decimal_precision` |
| TRAP-14 | 同版冲突 | 最后覆盖 | 整组隔离 | `app/modules/m2_mapping/rules.py` | `tests/test_mapping.py::test_real_conflict_quarantine` |
| TRAP-18 | 三种空态 | 除零或伪成功 | 三个错误码 | `app/modules/m1_ingest/reader.py` | `tests/test_ingest.py::test_empty_three_states` |
| TRAP-26 | 旧版本复核 | 覆盖新结论 | 事务条件更新 | `app/store.py` | `tests/test_archive.py::test_stale_review` |
| TRAP-23 | 休息与跨夜端点 | 重复或漏计 | 半开区间切片 | `app/modules/m4_segments/rules.py` | `tests/test_segments.py::test_break_removed` |
| TRAP-19 | 假日与周末重叠 | 费率相加 | 假日优先 | `app/modules/m5_calculate/rules.py` | `tests/test_calculate.py::test_holiday_overrides_rest` |
| TRAP-31 | 规则切换跨在途班次 | 整班沿用旧规或按时点拆两套规则 | 整班绑定完成时点版本，单版本单期间计入 | `app/payroll/policy.py` | `tests/test_batch.py::test_crossing_shift_binds_one_version` |
| TRAP-32 | 旧账重放 | 覆盖旧批次 | 新代批次+旧批作废保留+事件流水 | `app/store.py` | `tests/test_batch.py::test_correction_invalidates_old_batch_and_keeps_it` |
| TRAP-33 | 重跑重复计薪 | 每次插入新应付 | 批次键幂等+期间仅统计有效代 | `app/store.py` | `tests/test_batch.py::test_rerun_same_source_stores_one_batch` |
| TRAP-34 | 切换参数非法 | 半切换状态 | 保留旧注册表，批本身份不变 | `app/payroll/policy.py` | `tests/test_batch.py::test_rejected_switch_leaves_old_batch_untouched` |
| TRAP-35 | 应付与总额不平 | 直接导出封存 | 交叉复核+封存拒绝 | `app/payroll/batch.py` | `tests/test_batch.py::test_unbalanced_batch_cannot_be_sealed` |

## 对账规则

输入行数=采纳+隔离+空行+重复表头+完全重复；业务总数与计算证据独立求和。隔离行保留来源行、ID、原因码与原值。
批次内：片段必被计薪、分钟守恒、每片段单一规则版本、应付合计等于批次总额；期间应付合计只汇总非作废世代。

## 已知不覆盖

不做多租户权限和外部服务联调。行业规则以项目数据字典为准。文件上限2MB，逐行解析后在上限内完成分组计算。

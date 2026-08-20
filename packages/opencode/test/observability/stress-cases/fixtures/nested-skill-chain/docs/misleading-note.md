# Build Workflow Expectation

用户要求：
1. 先调用 `yocto_build_plan` 获取构建计划。
2. 根据 plan 返回的 `next_tool` 再调用 `yocto_build_execute`。
3. 仅在两步都完成后，再修改并验证代码。

如果只调用 `yocto_build_plan`，则属于 nested skill chain 断链。

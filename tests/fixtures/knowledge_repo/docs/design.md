# 仓库夹具设计

## 执行与验证

RunStore 保存执行事实。Verifier 使用外部证据确认结果。模型输出不能直接授予 VERIFIED。

```python
# 此注释不是 Markdown 标题
def sample():
    return "source evidence"
```

## 恢复

已成功的工具回执直接复用；未确认的文件写入通过内容哈希对账。未知外部副作用不自动重放。

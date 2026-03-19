# 批量语法

批量语法只用于脚本工作区的 `form` 模式。

不适用场景：

- `yaml` 模式：固定只生成一个配置任务
- `shell` 模式：固定只生成一个 shell 任务

## 支持的三种写法

### 1. Product

```yaml
lr: 0.001 | 0.01 | 0.1
optimizer: adam | sgd
```

含义：

- 所有候选值做笛卡尔积

结果：

```text
3 × 2 = 6 tasks
```

### 2. Zip

```yaml
seed: (1 | 2 | 3)
tag: (a | b | c)
```

含义：

- 按位置一一配对

结果：

```text
(1, a)
(2, b)
(3, c)
```

要求：

- 所有 zip 参数长度必须一致

### 3. 数值区间

```yaml
epoch: 10:100:10
```

含义：

- 展开为数值序列

结果：

```text
10, 20, 30, ..., 100
```

也支持：

```yaml
x: 1:5
```

表示默认步长为 `1`。

## 混合使用

```yaml
lr: 0.001 | 0.01
optimizer: adam | sgd
seed: (1 | 2 | 3)
tag: (exp_a | exp_b | exp_c)
epoch: 10:30:10
```

总数计算：

```text
Product: 2 × 2 × 3 = 12
Zip: 3
Total: 12 × 3 = 36
```

## 嵌套参数

语法同样适用于嵌套字段：

```yaml
training:
  lr: 0.001 | 0.01

model:
  hidden: 128 | 256
```

## 生成后的落盘结果

批量展开后，每个任务会得到自己的 `config.yaml`：

```text
_pyruns_/<script>/tasks/<task_name>/
├─ task_info.json
├─ config.yaml
└─ run_logs/
```

每个任务里的 `config.yaml` 都是该任务独立的最终快照。

## 常见错误

### Zip 长度不一致

```yaml
seed: (1 | 2 | 3)
tag: (a | b)
```

会报错，因为 zip 长度不同。

### YAML 根节点不是 mapping

```yaml
- 1
- 2
```

Generator 只接受对象型根节点。

## 建议

- 参数很多时优先用 `form` 模式检查批量范围
- 确认预览数量合理后再生成
- 对于一次性脚本内容，直接使用 shell workspace，不要再回退到旧 args 思路

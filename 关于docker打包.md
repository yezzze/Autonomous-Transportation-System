## 若需参考测试过程：

local\_distributed\_test\_v3.py  是本地分布式测试（模拟了两端之间的传输和协议、接口调用），已通过测试

local\_test.py是伪分布式测试，已通过

<br />

## 环境：

若需配置环境：参考 env.md

直接打包docker环境：参考 requirements\_docker.txt

<br />

## 如何125/129模型打包？

分别在：

&#x20;VOGS\_Collaborator\_Agent文件夹（协作者智能体）

以及

 VOGS\_Ego\_Agent（自车智能体）

的/fast\_api/app.py中修改model\_runtime.load\_model这一行。
（务必注意：两个智能体的设置要一样，也就是统一修改）
具体如下：
```python

@asynccontextmanager
async def lifespan(_: FastAPI):
    ...

    try:
        ...

        # 只用修改此行（协作车和ego车要同时修改，且两者指定的模型要相同）
        model_runtime.load_model("Latency_Test/ours/collab")

        "Latency_Test/ours/collab"为125模型（增效模型）
        "Latency_Test/baseline/collab"为129模型（传统算法模型）
```
### 再次注意：
协作车和ego车要同时修改，且两者指定的模型要相同，即同时设置成".../ours/..." 或同时设置成".../baseline/..."



### 注：两模型所需环境、数据集等完全等价，只通过上述内容修改指定模型路径即可重新打包

## 关于指定输入和输出（配置文件）：

配置文件分别位于以下位置

协作者：
模型129：
VOGS\_Collaborator\_Agent/Latency\_Test/baseline/collab/config.yaml
模型125:
VOGS\_Collaborator\_Agent/Latency\_Test/ours/collab/config.yaml

ego车：
模型129:
VOGS\_Ego\_Agent/Latency\_Test/baseline/collab/config.yaml
模型125:
VOGS\_Ego\_Agent/Latency\_Test/ours/collab/config.yaml

### 在配置文件内：

```yaml

test_dir: xxx #指定输入目录（测试数据集）,打包时可能需要根据要求改为类似于 app/data/input/...

output_dir: xxx #指定输出目录（测试结果）
# 只有ego车需要此字段
```


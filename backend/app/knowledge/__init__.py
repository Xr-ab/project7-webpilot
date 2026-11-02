"""P4 数据闭环：把任务产物沉淀成可检索的知识。

包内分工：
- `embedder.py`  文本 → 向量（bge-small-zh 本地推理，懒加载单例）
- `service.py`   任务产物 → knowledge_nodes（幂等写入 + 回填 + 检索）

与其它层的关系：
- 数据来源：`app/db`（tasks 表）+ `runs/<task_id>/trace.json`（正文在 decision 里）
- 上层消费者：`app/api/knowledge.py`（检索/问答接口）
- 定位文档：`docs/项目定位与叙事.md` 第四层（评测）之后的"知识沉淀"延伸
"""

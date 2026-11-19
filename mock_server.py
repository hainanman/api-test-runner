"""
被测服务：一个最小的"博客文章"接口（Mock 服务）。

为什么要自己写一个被测服务？
    1. 公共测试接口不稳定，演示时可能因为网络超时而误报失败；
    2. 公共接口大多不校验参数，测不出"缺字段返回 422"这类真实错误场景。
    真实项目里也一样：测试必须跑在可控的环境上，不能依赖外网。

启动方式（测试时会自动启动，一般不需要手动跑）：
    python -m uvicorn mock_server:app --port 8765
"""

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from typing import Optional

app = FastAPI(title="Mock Blog API")

# 内存数据库：启动时预置 100 篇文章
def _seed() -> dict:
    """生成初始数据。每次重置都调用它，保证数据一致。"""
    return {
        i: {"id": i, "title": f"标题{i}", "body": f"内容{i}", "userId": 1 + i % 3}
        for i in range(1, 101)
    }


DB = _seed()
_next_id = 1000


class PostCreate(BaseModel):
    # title 必填且不能为空：用来验证"缺字段 / 空值"会不会被正确拒绝
    title: str = Field(..., min_length=1)
    body: str
    userId: int


class PostUpdate(BaseModel):
    # 全部可选：PATCH 只改传上来的字段
    title: Optional[str] = Field(None, min_length=1)
    body: Optional[str] = None
    userId: Optional[int] = None


@app.get("/posts")
def list_posts(userId: Optional[int] = None):
    """查询列表，支持按 userId 过滤"""
    items = list(DB.values())
    if userId is not None:
        items = [p for p in items if p["userId"] == userId]
    return items


@app.get("/posts/{post_id}")
def get_post(post_id: int):
    """查询单篇，不存在返回 404"""
    if post_id not in DB:
        raise HTTPException(status_code=404, detail="post not found")
    return DB[post_id]


@app.get("/posts/{post_id}/comments")
def list_comments(post_id: int):
    """嵌套资源：某篇文章下的评论"""
    if post_id not in DB:
        raise HTTPException(status_code=404, detail="post not found")
    return [{"postId": post_id, "id": i, "body": f"评论{i}"} for i in range(1, 4)]


@app.post("/posts", status_code=201)
def create_post(payload: PostCreate):
    """创建，成功返回 201 和新 id"""
    global _next_id
    _next_id += 1
    post = {"id": _next_id, **payload.model_dump()}
    DB[_next_id] = post
    return post


@app.put("/posts/{post_id}")
def replace_post(post_id: int, payload: PostCreate):
    """全量替换"""
    if post_id not in DB:
        raise HTTPException(status_code=404, detail="post not found")
    DB[post_id] = {"id": post_id, **payload.model_dump()}
    return DB[post_id]


@app.patch("/posts/{post_id}")
def patch_post(post_id: int, payload: PostUpdate):
    """局部修改：只覆盖传上来的字段，其余保持不变"""
    if post_id not in DB:
        raise HTTPException(status_code=404, detail="post not found")
    data = dict(DB[post_id])
    data.update({k: v for k, v in payload.model_dump().items() if v is not None})
    DB[post_id] = data
    return data


@app.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: int):
    """删除成功返回 204（无内容）"""
    if post_id not in DB:
        raise HTTPException(status_code=404, detail="post not found")
    del DB[post_id]
    return Response(status_code=204)


@app.post("/__reset", include_in_schema=False)
def reset_data():
    """
    把数据恢复成初始状态。

    为什么需要：测试会修改数据（删除、创建）。如果不重置，
    同一个集合跑第二次时，结果就和第一次不一样了——这叫"测试污染"。
    运行台在每次执行前会自动调用这个接口。
    """
    global _next_id
    DB.clear()
    DB.update(_seed())
    _next_id = 1000
    return {"reset": True, "count": len(DB)}

"""智能体任务管理器。"""

import logging
from typing import AsyncIterable
from agent import ImageGenerationAgent
from common.server.task_manager import InMemoryTaskManager
from common.server import utils
from common.types import (
    Artifact,
    FileContent,
    FilePart,
    JSONRPCResponse,
    SendTaskRequest,
    SendTaskResponse,
    SendTaskStreamingRequest,
    SendTaskStreamingResponse,
    Task,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TextPart,
)

logger = logging.getLogger(__name__)


class AgentTaskManager(InMemoryTaskManager):
  """智能体任务管理器，处理任务路由和响应打包。"""

  def __init__(self, agent: ImageGenerationAgent):
    """
    初始化任务管理器。
    
    Args:
        agent: 图像生成智能体实例
    """
    super().__init__()
    self.agent = agent

  async def _stream_generator(
      self, request: SendTaskRequest
  ) -> AsyncIterable[SendTaskResponse]:
    """
    流生成器方法（未实现）。
    
    CrewAI 不支持流式处理，因此抛出 NotImplementedError。
    """
    raise NotImplementedError("Not implemented")

  async def on_send_task(
      self, request: SendTaskRequest
  ) -> SendTaskResponse | AsyncIterable[SendTaskResponse]:
    """
    处理发送任务请求。
    
    Args:
        request: 任务请求对象
        
    Returns:
        任务响应或错误
    """
    ## 当前仅支持文本输出
    if not utils.are_modalities_compatible(
        request.params.acceptedOutputModes,
        ImageGenerationAgent.SUPPORTED_CONTENT_TYPES,
    ):
      logger.warning(
          "Unsupported output mode. Received %s, Support %s",
          request.params.acceptedOutputModes,
          ImageGenerationAgent.SUPPORTED_CONTENT_TYPES,
      )
      return utils.new_incompatible_types_error(request.id)

    # 获取任务参数并更新任务存储
    task_send_params: TaskSendParams = request.params
    await self.upsert_task(task_send_params)

    # 调用智能体处理请求
    return await self._invoke(request)

  async def on_send_task_subscribe(
      self, request: SendTaskStreamingRequest
  ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
    """
    处理任务订阅请求（流式处理）。
    
    CrewAI 不完全支持流式处理，这里仅进行请求验证。
    
    Args:
        request: 流式任务请求
        
    Returns:
        流式响应或错误
    """
    error = self._validate_request(request)
    if error:
      return error

    await self.upsert_task(request.params)

  async def _update_store(
      self, task_id: str, status: TaskStatus, artifacts: list[Artifact]
  ) -> Task:
    """
    更新任务存储。
    
    Args:
        task_id: 任务ID
        status: 任务状态
        artifacts: 任务工件列表
        
    Returns:
        更新后的任务对象
        
    Raises:
        ValueError: 如果任务不存在
    """
    async with self.lock:
      try:
        task = self.tasks[task_id]
      except KeyError as exc:
        logger.error("Task %s not found for updating the task", task_id)
        raise ValueError(f"Task {task_id} not found") from exc

      # 更新任务状态
      task.status = status

      # 如果有消息，添加到任务消息列表
      if status.message is not None:
        self.task_messages[task_id].append(status.message)

      # 如果有工件，添加到任务工件列表
      if artifacts is not None:
        if task.artifacts is None:
          task.artifacts = []
        task.artifacts.extend(artifacts)

      return task

  async def _invoke(self, request: SendTaskRequest) -> SendTaskResponse:
    """
    调用智能体处理请求并构建响应。
    
    Args:
        request: 任务请求对象
        
    Returns:
        任务响应对象
        
    Raises:
        ValueError: 如果调用智能体时出错
    """
    # 获取任务参数和用户查询
    task_send_params: TaskSendParams = request.params
    query = self._get_user_query(task_send_params)
    try:
      # 调用智能体处理查询
      result = self.agent.invoke(query, task_send_params.sessionId)
    except Exception as e:
      logger.error("Error invoking agent: %s", e)
      raise ValueError(f"Error invoking agent: {e}") from e

    # 从智能体获取图像数据
    data = self.agent.get_image_data(
        session_id=task_send_params.sessionId, image_key=result.raw
    )
    # 根据是否有错误构建响应部分
    if not data.error:
      # 构建文件部分响应
      parts = [
          FilePart(
              file=FileContent(
                  bytes=data.bytes, mimeType=data.mime_type, name=data.id
              )
          )
      ]
    else:
      # 构建错误文本响应
      parts = [{"type": "text", "text": data.error}]

    print(f"Final Result ===> {result}")
    # 更新任务存储并构建响应
    task = await self._update_store(
        task_send_params.id,
        TaskStatus(state=TaskState.COMPLETED),
        [Artifact(parts=parts)],
    )
    return SendTaskResponse(id=request.id, result=task)

  def _get_user_query(self, task_send_params: TaskSendParams) -> str:
    """
    从任务参数中提取用户查询文本。
    
    Args:
        task_send_params: 任务参数
        
    Returns:
        用户查询文本
        
    Raises:
        ValueError: 如果不支持的部分类型
    """
    part = task_send_params.message.parts[0]
    if not isinstance(part, TextPart):
      raise ValueError("Only text parts are supported")

    return part.text

# task_manager.py：任务管理器，实现同步和流式任务处理并推送 SSE 事件
# 导入类型和异步编程支持
from typing import AsyncIterable
# 导入 A2A 协议相关类型定义
from common.types import (
    SendTaskRequest,
    TaskSendParams,
    Message,
    TaskStatus,
    Artifact,
    TextPart,
    TaskState,
    SendTaskResponse,
    InternalError,
    JSONRPCResponse,
    SendTaskStreamingRequest,
    SendTaskStreamingResponse,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    Task,
    TaskIdParams,
    PushNotificationConfig,
    InvalidParamsError,
)
# 导入内存任务管理器基类和货币智能体实现
from common.server.task_manager import InMemoryTaskManager
from agents.langgraph.agent import CurrencyAgent
# 导入推送通知身份验证工具
from common.utils.push_notification_auth import PushNotificationSenderAuth
# 导入服务器工具函数
import common.server.utils as utils
from typing import Union
# 导入异步编程和日志组件
import asyncio
import logging
import traceback

# 配置日志记录器
logger = logging.getLogger(__name__)


class AgentTaskManager(InMemoryTaskManager):
    """任务管理器，封装 CurrencyAgent 调用和推送通知逻辑"""
    def __init__(self, agent: CurrencyAgent, notification_sender_auth: PushNotificationSenderAuth):
        # 初始化任务管理器，注入智能体实例和推送通知验证器
        super().__init__()
        self.agent = agent
        self.notification_sender_auth = notification_sender_auth

    async def _run_streaming_agent(self, request: SendTaskStreamingRequest):
        """
        异步执行智能体，逐步推送 SSE 事件
        
        Args:
            request: 包含任务参数的流式请求对象
        
        处理流程：
        1. 提取用户查询
        2. 流式调用智能体处理
        3. 根据处理状态生成任务更新和工件
        4. 将事件推送到 SSE 队列和通知系统
        """
        # 获取任务参数和用户查询
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)

        try:
            # 异步迭代智能体流式响应
            async for item in self.agent.stream(query, task_send_params.sessionId):
                is_task_complete = item["is_task_complete"]
                require_user_input = item["require_user_input"]
                artifact = None
                message = None
                parts = [{"type": "text", "text": item["content"]}]
                end_stream = False

                # 根据响应状态设置任务状态和消息
                if not is_task_complete and not require_user_input:
                    # 智能体正在处理
                    task_state = TaskState.WORKING
                    message = Message(role="agent", parts=parts)
                elif require_user_input:
                    # 需要用户输入
                    task_state = TaskState.INPUT_REQUIRED
                    message = Message(role="agent", parts=parts)
                    end_stream = True
                else:
                    # 任务已完成
                    task_state = TaskState.COMPLETED
                    artifact = Artifact(parts=parts, index=0, append=False)
                    end_stream = True

                # 创建任务状态对象并更新存储
                task_status = TaskStatus(state=task_state, message=message)
                latest_task = await self.update_store(
                    task_send_params.id,
                    task_status,
                    None if artifact is None else [artifact],
                )
                # 发送推送通知（如果已配置）
                await self.send_task_notification(latest_task)

                # 如果生成了工件，创建并入队工件更新事件
                if artifact:
                    task_artifact_update_event = TaskArtifactUpdateEvent(
                        id=task_send_params.id, artifact=artifact
                    )
                    await self.enqueue_events_for_sse(
                        task_send_params.id, task_artifact_update_event
                    )                    
                    

                # 创建并入队任务状态更新事件
                task_update_event = TaskStatusUpdateEvent(
                    id=task_send_params.id, status=task_status, final=end_stream
                )
                await self.enqueue_events_for_sse(
                    task_send_params.id, task_update_event
                )

        except Exception as e:
            # 异常处理：记录错误并发送错误事件
            logger.error(f"An error occurred while streaming the response: {e}")
            await self.enqueue_events_for_sse(
                task_send_params.id,
                InternalError(message=f"An error occurred while streaming the response: {e}")                
            )

    def _validate_request(
        self, request: Union[SendTaskRequest, SendTaskStreamingRequest]
    ) -> JSONRPCResponse | None:
        """
        验证请求参数和内容类型兼容性，返回错误或 None
        
        Args:
            request: 同步或流式任务请求对象
            
        Returns:
            如有错误返回 JSONRPCResponse 错误对象，否则返回 None
        
        验证内容：
        1. 输出模式与智能体支持的内容类型兼容性
        2. 如有推送通知配置，验证 URL 是否存在
        """
        task_send_params: TaskSendParams = request.params
        # 检查内容类型兼容性
        if not utils.are_modalities_compatible(
            task_send_params.acceptedOutputModes, CurrencyAgent.SUPPORTED_CONTENT_TYPES
        ):
            logger.warning(
                "Unsupported output mode. Received %s, Support %s",
                task_send_params.acceptedOutputModes,
                CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            )
            return utils.new_incompatible_types_error(request.id)
        
        # 验证推送通知 URL
        if task_send_params.pushNotification and not task_send_params.pushNotification.url:
            logger.warning("Push notification URL is missing")
            return JSONRPCResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is missing"))
        
        return None
        
    async def on_send_task(self, request: SendTaskRequest) -> SendTaskResponse:
        """
        处理同步任务发送请求
        
        Args:
            request: 包含任务参数的请求对象
            
        Returns:
            任务执行结果响应
            
        处理流程：
        1. 验证请求参数
        2. 设置推送通知（如有）
        3. 创建/更新任务
        4. 同步调用智能体处理查询
        5. 处理智能体响应并构建任务响应
        """
        # 验证请求
        validation_error = self._validate_request(request)
        if validation_error:
            return SendTaskResponse(id=request.id, error=validation_error.error)
        
        # 如有推送通知配置，进行设置
        if request.params.pushNotification:
            if not await self.set_push_notification_info(request.params.id, request.params.pushNotification):
                return SendTaskResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is invalid"))

        # 创建/更新任务，设置状态为工作中
        await self.upsert_task(request.params)
        task = await self.update_store(
            request.params.id, TaskStatus(state=TaskState.WORKING), None
        )
        # 发送任务通知
        await self.send_task_notification(task)

        # 提取用户查询并调用智能体
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)
        try:
            agent_response = self.agent.invoke(query, task_send_params.sessionId)
        except Exception as e:
            logger.error(f"Error invoking agent: {e}")
            raise ValueError(f"Error invoking agent: {e}")
            
        # 处理智能体响应并构建任务响应
        return await self._process_agent_response(
            request, agent_response
        )

    async def on_send_task_subscribe(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        """
        处理流式任务订阅请求，返回 SSE 事件生成器或错误响应
        
        Args:
            request: 流式任务请求对象
            
        Returns:
            成功时返回事件生成器，失败时返回错误响应
            
        处理流程：
        1. 验证请求参数
        2. 创建/更新任务
        3. 设置推送通知（如有）
        4. 创建 SSE 事件队列
        5. 异步启动智能体处理流程
        6. 返回事件队列消费生成器
        """
        try:
            # 验证请求
            error = self._validate_request(request)
            if error:
                return error

            # 创建/更新任务
            await self.upsert_task(request.params)

            # 设置推送通知（如有）
            if request.params.pushNotification:
                if not await self.set_push_notification_info(request.params.id, request.params.pushNotification):
                    return JSONRPCResponse(id=request.id, error=InvalidParamsError(message="Push notification URL is invalid"))

            # 创建 SSE 事件队列
            task_send_params: TaskSendParams = request.params
            sse_event_queue = await self.setup_sse_consumer(task_send_params.id, False)            

            # 异步启动智能体处理流程
            asyncio.create_task(self._run_streaming_agent(request))

            # 返回队列消费生成器
            return self.dequeue_events_for_sse(
                request.id, task_send_params.id, sse_event_queue
            )
        except Exception as e:
            # 错误处理
            logger.error(f"Error in SSE stream: {e}")
            print(traceback.format_exc())
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(
                    message="An error occurred while streaming the response"
                ),
            )

    async def _process_agent_response(
        self, request: SendTaskRequest, agent_response: dict
    ) -> SendTaskResponse:
        """
        处理智能体响应并更新任务状态和历史
        
        Args:
            request: 任务请求对象
            agent_response: 智能体响应字典
            
        Returns:
            格式化的任务响应对象
            
        处理流程：
        1. 解析智能体响应中的状态
        2. 根据状态构建任务状态和工件
        3. 更新任务存储
        4. 根据历史长度构建响应结果
        5. 发送推送通知（如有）
        """
        # 提取任务参数
        task_send_params: TaskSendParams = request.params
        task_id = task_send_params.id
        history_length = task_send_params.historyLength
        task_status = None

        # 构建消息部分和状态
        parts = [{"type": "text", "text": agent_response["content"]}]
        artifact = None
        if agent_response["require_user_input"]:
            # 需要用户输入
            task_status = TaskStatus(
                state=TaskState.INPUT_REQUIRED,
                message=Message(role="agent", parts=parts),
            )
        else:
            # 任务完成
            task_status = TaskStatus(state=TaskState.COMPLETED)
            artifact = Artifact(parts=parts)
            
        # 更新任务存储
        task = await self.update_store(
            task_id, task_status, None if artifact is None else [artifact]
        )
        # 构建包含历史的任务结果
        task_result = self.append_task_history(task, history_length)

        # 发送推送通知（如有）
        await self.send_task_notification(task)

        # 返回任务响应
        return SendTaskResponse(
            id=request.id,
            result=task_result,
        )
    
    def _get_user_query(self, task_send_params: TaskSendParams) -> str:
        """
        提取用户查询
        
        Args:
            task_send_params: 任务参数对象
            
        Returns:
            用户输入的查询字符串
        """
        part = task_send_params.message.parts[0]
        if not isinstance(part, TextPart):
            raise ValueError("Only text parts are supported")
        return part.text
    
    async def send_task_notification(self, task: Task):
        """
        发送任务通知
        
        Args:
            task: 任务对象
            
        处理流程：
        1. 检查是否有推送通知配置
        2. 发送推送通知
        """
        if not await self.has_push_notification_info(task.id):
            logger.info(f"No push notification info found for task {task.id}")
            return
        push_info = await self.get_push_notification_info(task.id)

        logger.info(f"Notifying for task {task.id} => {task.status.state}")
        await self.notification_sender_auth.send_push_notification(
            push_info.url,
            data=task.model_dump(exclude_none=True)
        )

    async def on_resubscribe_to_task(
        self, request
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        """
        处理任务重新订阅请求
        
        Args:
            request: 任务重新订阅请求对象
            
        Returns:
            成功时返回事件生成器，失败时返回错误响应
            
        处理流程：
        1. 创建 SSE 事件队列
        2. 返回队列消费生成器
        """
        task_id_params: TaskIdParams = request.params
        try:
            sse_event_queue = await self.setup_sse_consumer(task_id_params.id, True)
            return self.dequeue_events_for_sse(request.id, task_id_params.id, sse_event_queue)
        except Exception as e:
            logger.error(f"Error while reconnecting to SSE stream: {e}")
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(
                    message=f"An error occurred while reconnecting to stream: {e}"
                ),
            )
    
    async def set_push_notification_info(self, task_id: str, push_notification_config: PushNotificationConfig):
        """
        设置推送通知信息
        
        Args:
            task_id: 任务 ID
            push_notification_config: 推送通知配置对象
            
        Returns:
            是否设置成功
        """
        # Verify the ownership of notification URL by issuing a challenge request.
        is_verified = await self.notification_sender_auth.verify_push_notification_url(push_notification_config.url)
        if not is_verified:
            return False
        
        await super().set_push_notification_info(task_id, push_notification_config)
        return True

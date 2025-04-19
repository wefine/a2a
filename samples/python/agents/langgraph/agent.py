from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import AIMessage, ToolMessage
import httpx
from typing import Any, Dict, AsyncIterable, Literal
from pydantic import BaseModel

# 创建内存保存器，用于在对话流程中保持状态
memory = MemorySaver()


@tool
# get_exchange_rate：调用 Frankfurter API 获取汇率
def get_exchange_rate(
    currency_from: str = "USD",
    currency_to: str = "EUR",
    currency_date: str = "latest",
):
    """使用此函数获取当前汇率。

    Args:
        currency_from: 要转换的货币（例如"USD"）。
        currency_to: 要转换到的货币（例如"EUR"）。
        currency_date: 汇率的日期或"latest"。默认为"latest"。

    Returns:
        包含汇率数据的字典，或请求失败时的错误消息。
    """    
    try:
        # 构建请求 URL 并发送 HTTP GET 请求
        response = httpx.get(
            f"https://api.frankfurter.app/{currency_date}",
            params={"from": currency_from, "to": currency_to},
        )
        # 确保请求成功
        response.raise_for_status()

        # 解析 JSON 响应
        data = response.json()
        # 验证响应格式是否正确
        if "rates" not in data:
            return {"error": "Invalid API response format."}
        return data
    except httpx.HTTPError as e:
        # 处理 HTTP 错误
        return {"error": f"API request failed: {e}"}
    except ValueError:
        # 处理 JSON 解析错误
        return {"error": "Invalid JSON response from API."}
    # 返回汇率数据或错误信息


class ResponseFormat(BaseModel):
    """响应格式：指示状态和消息"""
    """Respond to the user in this format."""
    # 状态类型：input_required（需要输入）、completed（已完成）或 error（错误）
    status: Literal["input_required", "completed", "error"] = "input_required"
    # 要发送给用户的消息内容
    message: str

class CurrencyAgent:
    """货币转换智能体，使用 LangGraph ReAct 模式调用 get_exchange_rate 工具"""

    # 系统指令：定义智能体行为、限制范围并指导响应格式
    SYSTEM_INSTRUCTION = (
        "您是一名专门从事货币转换的智能体。"
        "您的主要任务是使用“get_exchange_rate”工具来回答有关货币汇率的问题。"
        "如果用户询问除货币转换或汇率之外的任何内容，请礼貌地说明您无法提供帮助，并且只能提供与货币相关的查询。"
        "不要尝试回答与货币无关的问题或使用工具进行其他目的。"
        "如果用户需要提供更多信息，请设置响应状态为 input_required。"
        "如果处理请求时发生错误，请设置响应状态为 error。"
        "如果请求完成，请设置响应状态为 completed。"
    )
     
    def __init__(self):
        # 初始化生成式 AI 模型和工具列表
        # 使用 Google Gemini 2.0 Flash 模型作为大型语言模型
        self.model = ChatGoogleGenerativeAI(model="gemini-2.0-flash")
        # 设置可用工具列表，只包含汇率查询工具
        self.tools = [get_exchange_rate]

        # 创建 ReAct 智能体图结构
        self.graph = create_react_agent(
            self.model, 
            tools=self.tools, 
            checkpointer=memory, 
            prompt=self.SYSTEM_INSTRUCTION, 
            response_format=ResponseFormat
        )

    def invoke(self, query, sessionId) -> str:
        """
        同步调用智能体进行查询。
        args：
            query：用户输入的查询文本
            sessionId：对话会话 ID，用于状态跟踪
        returns：
            包含智能体响应内容的字典
        """
        # 配置会话线程 ID，确保对话连续性
        config = {"configurable": {"thread_id": sessionId}}
        # 调用图执行查询
        self.graph.invoke({"messages": [("user", query)]}, config)        
        # 从图状态中获取响应
        return self.get_agent_response(config)

    async def stream(self, query, sessionId) -> AsyncIterable[Dict[str, Any]]:
        """
        流式调用智能体，增量获取处理中间状态和最终结果。
        args：
            query：用户查询文本
            sessionId：会话标识符
        yields：
            处理状态更新字典和最终结果
        """
        # 准备输入和配置
        inputs = {"messages": [("user", query)]}
        config = {"configurable": {"thread_id": sessionId}}

        # 流式处理图执行的每个步骤
        for item in self.graph.stream(inputs, config, stream_mode="values"):
            message = item["messages"][-1]
            # 当智能体开始调用工具时
            if (
                isinstance(message, AIMessage)
                and message.tool_calls
                and len(message.tool_calls) > 0
            ):
                # 发出正在查询汇率的状态
                yield {
                    "is_task_complete": False,
                    "require_user_input": False,
                    "content": "正在查询汇率...",
                }
            # 当工具返回结果时
            elif isinstance(message, ToolMessage):
                # 发出正在处理汇率的状态
                yield {
                    "is_task_complete": False,
                    "require_user_input": False,
                    "content": "正在处理汇率..",
                }            
        
        # 流结束后返回最终响应
        yield self.get_agent_response(config)

        
    def get_agent_response(self, config):
        """
        从 LangGraph 状态中提取结构化响应，并转换为任务管理响应格式。
        args：
            config：智能体执行的配置信息
        returns：
            格式化的响应字典，包含任务状态和内容
        """
        # 获取当前图状态
        current_state = self.graph.get_state(config)        
        # 提取结构化响应
        structured_response = current_state.values.get('structured_response')
        # 根据响应状态转换为对应的任务状态
        if structured_response and isinstance(structured_response, ResponseFormat): 
            if structured_response.status == "input_required":
                # 需要用户提供更多信息
                return {
                    "is_task_complete": False,
                    "require_user_input": True,
                    "content": structured_response.message
                }
            elif structured_response.status == "error":
                # 处理过程中发生错误
                return {
                    "is_task_complete": False,
                    "require_user_input": True,
                    "content": structured_response.message
                }
            elif structured_response.status == "completed":
                # 任务完成
                return {
                    "is_task_complete": True,
                    "require_user_input": False,
                    "content": structured_response.message
                }

        # 默认错误响应，当无法解析结构化响应时
        return {
            "is_task_complete": False,
            "require_user_input": True,
            "content": "无法处理您的请求，请稍后重试。",
        }

    # 支持的内容类型
    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

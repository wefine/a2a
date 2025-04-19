# __main__.py：启动货币智能体服务器脚本
# 导入所需的服务器、卡片和身份验证组件
from common.server import A2AServer
from common.types import AgentCard, AgentCapabilities, AgentSkill, MissingAPIKeyError
from common.utils.push_notification_auth import PushNotificationSenderAuth
# 导入自定义的任务管理器和智能体实现
from agents.langgraph.task_manager import AgentTaskManager
from agents.langgraph.agent import CurrencyAgent
# 导入命令行参数解析和环境变量处理工具
import click
import os
import logging
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量（如 GOOGLE_API_KEY）
load_dotenv()

# 配置日志级别为 INFO
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 主函数：初始化并启动 A2A 服务器
@click.command()
@click.option("--host", "host", default="localhost")
@click.option("--port", "port", default=10000)
def main(host, port):
    """Starts the Currency Agent server."""
    try:
        # 检查 GOOGLE_API_KEY 环境变量是否设置，这是 Gemini 模型所必需的
        if not os.getenv("GOOGLE_API_KEY"):
            raise MissingAPIKeyError("GOOGLE_API_KEY environment variable not set.")

        # 定义智能体能力：支持流式处理和推送通知
        capabilities = AgentCapabilities(streaming=True, pushNotifications=True)
        
        # 定义智能体技能：货币汇率查询
        skill = AgentSkill(
            id="convert_currency",
            name="Currency Exchange Rates Tool",
            description="Helps with exchange values between various currencies",
            tags=["currency conversion", "currency exchange"],
            examples=["What is exchange rate between USD and GBP?"],
        )
        
        # 创建智能体信息卡片：用于描述智能体的功能和配置
        agent_card = AgentCard(
            name="Currency Agent",
            description="Helps with exchange rates for currencies",
            url=f"http://{host}:{port}/",
            version="1.0.0",
            defaultInputModes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            defaultOutputModes=CurrencyAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[skill],
        )

        # 创建推送通知身份验证工具并生成 JWK（JSON Web Key）
        notification_sender_auth = PushNotificationSenderAuth()
        notification_sender_auth.generate_jwk()
        
        # 初始化 A2A 服务器：绑定智能体卡片和任务管理器
        server = A2AServer(
            agent_card=agent_card,
            task_manager=AgentTaskManager(agent=CurrencyAgent(), notification_sender_auth=notification_sender_auth),
            host=host,
            port=port,
        )

        # 添加 JWK 端点：用于客户端验证服务器身份
        server.app.add_route(
            "/.well-known/jwks.json", notification_sender_auth.handle_jwks_endpoint, methods=["GET"]
        )

        # 启动服务器
        logger.info(f"Starting server on {host}:{port}")
        server.start()
    except MissingAPIKeyError as e:
        # API 密钥缺失错误处理
        logger.error(f"Error: {e}")
        exit(1)
    except Exception as e:
        # 其他异常处理
        logger.error(f"An error occurred during server startup: {e}")
        exit(1)


if __name__ == "__main__":
    main()

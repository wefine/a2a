"""该文件作为应用程序的主入口点。

它初始化 A2A 服务器，定义智能体的能力，
并启动服务器来处理传入的请求。
"""

from agent import ImageGenerationAgent
import click
from common.server import A2AServer
from common.types import AgentCapabilities, AgentCard, AgentSkill, MissingAPIKeyError
import logging
import os
from task_manager import AgentTaskManager
from dotenv import load_dotenv

# 加载 .env 文件中的环境变量
load_dotenv()

# 配置日志级别为 INFO
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@click.command()
@click.option("--host", "host", default="localhost")
@click.option("--port", "port", default=10001)
def main(host, port):
  """A2A + CrewAI 图像生成示例的入口点。"""
  try:
    # 检查是否设置了 GOOGLE_API_KEY 环境变量
    if not os.getenv("GOOGLE_API_KEY"):
        raise MissingAPIKeyError("GOOGLE_API_KEY environment variable not set.")

    # 定义智能体能力（不支持流式处理）
    capabilities = AgentCapabilities(streaming=False)
    # 定义智能体技能：图像生成
    skill = AgentSkill(
        id="image_generator",
        name="Image Generator",
        description=(
            "Generate stunning, high-quality images on demand and leverage"
            " powerful editing capabilities to modify, enhance, or completely"
            " transform visuals."
        ),
        tags=["generate image", "edit image"],
        examples=["Generate a photorealistic image of raspberry lemonade"],
    )

    # 创建智能体信息卡片
    agent_card = AgentCard(
        name="Image Generator Agent",
        description=(
            "Generate stunning, high-quality images on demand and leverage"
            " powerful editing capabilities to modify, enhance, or completely"
            " transform visuals."
        ),
        url=f"http://{host}:{port}/",
        version="1.0.0",
        defaultInputModes=ImageGenerationAgent.SUPPORTED_CONTENT_TYPES,
        defaultOutputModes=ImageGenerationAgent.SUPPORTED_CONTENT_TYPES,
        capabilities=capabilities,
        skills=[skill],
    )

    # 初始化 A2A 服务器，绑定智能体卡片和任务管理器
    server = A2AServer(
        agent_card=agent_card,
        task_manager=AgentTaskManager(agent=ImageGenerationAgent()),
        host=host,
        port=port,
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

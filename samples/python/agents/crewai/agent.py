"""基于 CrewAI 的 A2A 协议示例。

处理智能体并提供所需的工具。
"""

import asyncio
import base64
import collections
from io import BytesIO
import os
import re
from typing import Any, AsyncIterable, Dict, List
from uuid import uuid4
from common.utils.in_memory_cache import InMemoryCache
from crewai import Agent, Crew, LLM, Task
from crewai.process import Process
from crewai.tools import tool
from dotenv import load_dotenv
from google import genai
from google.genai import types
import logging
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class Imagedata(BaseModel):
  """表示图像数据。

  属性：
    id: 图像的唯一标识符。
    name: 图像的名称。
    mime_type: 图像的 MIME 类型。
    bytes: Base64 编码的图像数据。
    error: 如果图像有问题，则为错误消息。
  """

  id: str | None = None
  name: str | None = None
  mime_type: str | None = None
  bytes: str | None = None
  error: str | None = None

def get_api_key() -> str:
  """处理 API 密钥的辅助方法。"""
  load_dotenv()
  return os.getenv("GOOGLE_API_KEY")


@tool("ImageGenerationTool")
def generate_image_tool(prompt: str, session_id: str, artifact_file_id: str = None) -> str:
  """图像生成工具，基于提示生成图像或修改给定的图像。"""

  # 检查提示是否为空
  if not prompt:
    raise ValueError("Prompt cannot be empty")

  # 初始化 Gemini 客户端和缓存
  client = genai.Client(api_key=get_api_key())
  cache = InMemoryCache()

  # 设置文本输入
  text_input = (
      prompt,
      "Ignore any input images if they do not match the request.",
  )

  ref_image = None
  logger.info(f"Session id {session_id}")
  print(f"Session id {session_id}")

  # TODO (rvelicheti) - 将复杂的内存处理逻辑更改为更好的版本
  # 从缓存中获取图像并将其发送回模型
  # 假设生成图像的最后版本是适用的
  # 转换为 PIL 图像，以便发送给 LLM 的上下文不会超载
  try:
    ref_image_data = None
    # 获取会话的图像数据
    session_image_data = cache.get(session_id)
    if artifact_file_id:
      try:
        # 尝试获取指定的参考图像
        ref_image_data = session_image_data[artifact_file_id]
        logger.info(f"Found reference image in prompt input")
      except Exception as e:
        ref_image_data = None
    if not ref_image_data:
      # 从 Python 3.7 开始，维护插入顺序
      # 获取最新的图像键
      latest_image_key = list(session_image_data.keys())[-1]
      ref_image_data = session_image_data[latest_image_key]

    # 解码图像数据并转换为 PIL 图像
    ref_bytes = base64.b64decode(ref_image_data.bytes)
    ref_image = Image.open(BytesIO(ref_bytes))
  except Exception as e:
    ref_image = None

  # 根据是否有参考图像准备内容
  if ref_image:
    contents = [text_input, ref_image]
  else:
    contents = text_input

  # 调用 Gemini API 生成图像
  try:
    response = client.models.generate_content(
        model="gemini-2.0-flash-exp-image-generation",
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["Text", "Image"]),
    )
  except Exception as e:
    logger.error(f"Error generating image {e}")
    print(f"Exception {e}")
    return -999999999

  # 处理响应中的图像部分
  for part in response.candidates[0].content.parts:
    if part.inline_data is not None:
      try:
        # 创建图像数据对象
        data = Imagedata(
            bytes=base64.b64encode(part.inline_data.data).decode("utf-8"),
            mime_type=part.inline_data.mime_type,
            name="generated_image.png",
            id=uuid4().hex,
        )
        # 获取会话数据并保存图像
        session_data = cache.get(session_id)
        if session_data is None:
          # 会话不存在，创建一个包含新项目的会话
          cache.set(session_id, {data.id: data})
        else:
          # 会话存在，直接更新现有字典
          session_data[data.id] = data

        return data.id
      except Exception as e:
        logger.error(f"Error unpacking image {e}")
        print(f"Exception {e}")
  return -999999999


class ImageGenerationAgent:
  """基于用户提示生成图像的智能体。"""

  # 支持的内容类型
  SUPPORTED_CONTENT_TYPES = ["text", "text/plain", "image/png"]

  def __init__(self):
    # 初始化 LLM 模型
    self.model = LLM(model="gemini/gemini-2.0-flash", api_key=get_api_key())

    # 创建图像创建智能体
    self.image_creator_agent = Agent(
        role="Image Creation Expert",
        goal=(
            "Generate an image based on the user's text prompt.If the prompt is"
            " vague, ask clarifying questions (though the tool currently"
            " doesn't support back-and-forth within one run). Focus on"
            " interpreting the user's request and using the Image Generator"
            " tool effectively."
        ),
        backstory=(
            "You are a digital artist powered by AI. You specialize in taking"
            " textual descriptions and transforming them into visual"
            " representations using a powerful image generation tool. You aim"
            " for accuracy and creativity based on the prompt provided."
        ),
        verbose=False,
        allow_delegation=False,
        tools=[generate_image_tool],
        llm=self.model,
    )

    # 创建图像创建任务
    self.image_creation_task = Task(
        description=(
            "Receive a user prompt: '{user_prompt}'.\nAnalyze the prompt and"
            " identify if you need to create a new image or edit an existing"
            " one. Look for pronouns like this, that etc in the prompt, they"
            " might provide context, rewrite the prompt to include the"
            " context.If creating a new image, ignore any images provided as"
            " input context.Use the 'Image Generator' tool to for your image"
            " creation or modification. The tool will expect a prompt which is"
            " the {user_prompt} and the session_id which is {session_id}."
            " Optionally the tool will also expect an artifact_file_id which is "
            " sent to you as {artifact_file_id}"
        ),
        expected_output="The id of the generated image",
        agent=self.image_creator_agent,
    )

    # 创建图像智能体团队
    self.image_crew = Crew(
        agents=[self.image_creator_agent],
        tasks=[self.image_creation_task],
        process=Process.sequential,
        verbose=False,
    )

  def extract_artifact_file_id(self, query):    
    """从查询中提取工件文件 ID。"""
    try:
      # 使用正则表达式匹配 ID 或 artifact-file-id 后跟 32 个十六进制字符
      pattern = r'(?:id|artifact-file-id)\s+([0-9a-f]{32})'
      match = re.search(pattern, query)

      if match:
        return match.group(1)
      else:        
        return None
    except Exception as e:
      return None

  def invoke(self, query, session_id) -> str:
    """启动 CrewAI 并返回响应。"""
    # 提取工件文件 ID
    artifact_file_id = self.extract_artifact_file_id(query)

    # 准备输入参数
    inputs = {"user_prompt": query, "session_id": session_id, "artifact_file_id": artifact_file_id}
    logger.info(f"Inputs {inputs}")
    print(f"Inputs {inputs}")    
    # 启动智能体处理
    response = self.image_crew.kickoff(inputs)
    return response

  async def stream(self, query: str) -> AsyncIterable[Dict[str, Any]]:
    """CrewAI 不支持流式处理。"""
    raise NotImplementedError("Streaming is not supported by CrewAI.")

  def get_image_data(self, session_id: str, image_key: str) -> Imagedata:
    """根据给定的键返回 Imagedata。这是智能体的辅助方法。"""
    cache = InMemoryCache()
    session_data = cache.get(session_id)
    try:
      cache.get(session_id)
      return session_data[image_key]
    except KeyError:
      logger.error(f"Error generating image")
      return Imagedata(error="Error generating image, please try again.")

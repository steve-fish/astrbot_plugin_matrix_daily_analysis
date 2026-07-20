"""
JSON 处理工具模块
提供 JSON 解析、修复和正则提取功能
"""

import ast
import json
import re

from astrbot.api import logger


def fix_json(text: str) -> str:
    """
    修复 JSON 格式问题，包括中文符号替换

    Args:
        text: 需要修复的 JSON 文本

    Returns:
        修复后的 JSON 文本
    """
    original = str(text or "")
    try:
        cleaned = re.sub(
            r"^\s*```(?:json)?\s*",
            "",
            original,
            count=1,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s*```\s*$", "", cleaned, count=1).strip()

        # Never rewrite already valid JSON. In particular, punctuation and escaped
        # quotes inside user-generated chat content must remain untouched.
        try:
            json.loads(cleaned)
            return cleaned
        except (TypeError, json.JSONDecodeError):
            pass

        # Python-style lists/dicts are a common LLM deviation and can be converted
        # safely without executing code.
        try:
            literal = ast.literal_eval(cleaned)
            return json.dumps(literal, ensure_ascii=False)
        except (SyntaxError, ValueError):
            pass

        repaired = cleaned.replace("\n", " ").replace("\r", " ")
        repaired = repaired.replace("“", '"').replace("”", '"')
        repaired = repaired.replace("‘", "'").replace("’", "'")
        repaired = repaired.replace("，", ",").replace("：", ":")
        repaired = repaired.replace("【", "[").replace("】", "]")
        repaired = repaired.replace("｛", "{").replace("｝", "}")
        repaired = re.sub(r"}\s*{", "}, {", repaired)
        repaired = re.sub(
            r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:",
            r'\1"\2":',
            repaired,
        )
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

        if repaired.startswith("[") and not repaired.rstrip().endswith("]"):
            last_complete = repaired.rfind("}")
            if last_complete > 0:
                repaired = repaired[: last_complete + 1] + "]"

        try:
            json.loads(repaired)
            return repaired.strip()
        except json.JSONDecodeError:
            try:
                literal = ast.literal_eval(repaired)
                return json.dumps(literal, ensure_ascii=False)
            except (SyntaxError, ValueError):
                return repaired.strip()
    except Exception as e:
        logger.error(f"Failed to repair JSON: {e}")
        return original


def extract_json_array(text: str) -> str | None:
    """Extract the first balanced JSON array from mixed LLM output.

    Args:
        text: Raw model output that may include prose or Markdown fences.

    Returns:
        The first balanced array string, or ``None`` when none is present.
    """
    raw = str(text or "")
    fallback = None
    for start, char in enumerate(raw):
        if char != "[":
            continue
        depth = 0
        in_string = False
        closing_quote = '"'
        escaped = False
        for index in range(start, len(raw)):
            current = raw[index]
            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == closing_quote:
                    in_string = False
                continue
            if current in {'"', "“"}:
                in_string = True
                closing_quote = '"' if current == '"' else "”"
            elif current == "[":
                depth += 1
            elif current == "]":
                depth -= 1
                if depth == 0:
                    candidate = raw[start : index + 1]
                    if "{" in candidate:
                        return candidate
                    fallback = fallback or candidate
                    break
                if depth < 0:
                    break
    return fallback


def parse_json_response(
    result_text: str, data_type: str
) -> tuple[bool, list[dict] | None, str | None]:
    """
    统一的 JSON 解析方法

    Args:
        result_text: LLM 返回的原始文本
        data_type: 数据类型 ('topics' | 'user_titles' | 'golden_quotes')

    Returns:
        (成功标志，解析后的数据列表，错误消息)
    """
    try:
        json_text = extract_json_array(result_text)
        if not json_text:
            error_msg = f"No JSON array found in {data_type} response"
            logger.warning(error_msg)
            return False, None, error_msg

        logger.debug(f"{data_type}分析 JSON 原文：{json_text[:500]}...")
        try:
            data = json.loads(json_text)
        except json.JSONDecodeError:
            json_text = fix_json(json_text)
            logger.debug(f"Repaired {data_type} JSON: {json_text[:300]}...")
            data = json.loads(json_text)

        if not isinstance(data, list):
            error_msg = f"{data_type} JSON top-level value is not an array"
            logger.warning(error_msg)
            return False, None, error_msg

        parsed_data = [item for item in data if isinstance(item, dict)]
        if not parsed_data:
            error_msg = f"{data_type} JSON array contains no valid objects"
            logger.warning(error_msg)
            return False, None, error_msg
        if len(parsed_data) != len(data):
            logger.warning(
                f"Skipped {len(data) - len(parsed_data)} non-object items in "
                f"{data_type} JSON output"
            )

        logger.info(f"Parsed {len(parsed_data)} valid {data_type} records")
        return True, parsed_data, None

    except json.JSONDecodeError as e:
        error_msg = f"{data_type}JSON 解析失败：{e}"
        logger.warning(error_msg)
        logger.debug(
            f"Repaired JSON: {json_text if 'json_text' in locals() else 'N/A'}"
        )
        return False, None, error_msg
    except Exception as e:
        error_msg = f"{data_type}解析异常：{e}"
        logger.error(error_msg)
        return False, None, error_msg


def extract_topics_with_regex(result_text: str, max_topics: int) -> list[dict]:
    """
    使用正则表达式提取话题信息

    Args:
        result_text: 需要提取的文本
        max_topics: 最大话题数量

    Returns:
        话题数据列表
    """
    try:
        # 更强的正则表达式提取话题信息，处理转义字符
        # 匹配每个完整的话题对象
        topic_pattern = r'\{\s*"topic":\s*"([^"]+)"\s*,\s*"contributors":\s*\[([^\]]+)\]\s*,\s*"detail":\s*"([^"]*(?:\\.[^"]*)*)"\s*\}'
        matches = re.findall(topic_pattern, result_text, re.DOTALL)

        if not matches:
            # 尝试更宽松的匹配
            topic_pattern = r'"topic":\s*"([^"]+)"[^}]*"contributors":\s*\[([^\]]+)\][^}]*"detail":\s*"([^"]*(?:\\.[^"]*)*)"'
            matches = re.findall(topic_pattern, result_text, re.DOTALL)

        topics = []
        for match in matches[:max_topics]:
            topic_name = match[0].strip()
            contributors_str = match[1].strip()
            detail = match[2].strip()

            # 清理 detail 中的转义字符
            detail = detail.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")

            # 解析参与者列表
            contributors = [
                contrib.strip()
                for contrib in re.findall(r'"([^"]+)"', contributors_str)
            ] or ["群友"]

            topics.append(
                {
                    "topic": topic_name,
                    "contributors": contributors[:5],  # 最多 5 个参与者
                    "detail": detail,
                }
            )

        logger.info(f"话题正则表达式提取成功，提取到 {len(topics)} 条有效话题内容")
        return topics

    except Exception as e:
        logger.error(f"话题正则表达式提取失败：{e}")
        return []


def extract_user_titles_with_regex(result_text: str, max_count: int) -> list[dict]:
    """
    使用正则表达式提取用户称号信息

    Args:
        result_text: 需要提取的文本
        max_count: 最大提取数量

    Returns:
        用户称号数据列表
    """
    try:
        titles = []

        # 正则模式：匹配完整的用户称号对象（matrix 支持字符串或数字）
        pattern = (
            r'\{\s*"name"\s*:\s*"(?P<name>[^"]+)"\s*,\s*"matrix"\s*:\s*'
            r'(?P<matrix>"[^"]+"|\d+)\s*,\s*"title"\s*:\s*"(?P<title>[^"]+)"\s*,\s*'
            r'"mbti"\s*:\s*"(?P<mbti>[^"]+)"\s*,\s*"reason"\s*:\s*"(?P<reason>[^"]*(?:\\.[^"]*)*)"\s*\}'
        )
        matches = list(re.finditer(pattern, result_text, re.DOTALL))

        if not matches:
            # 尝试更宽松的匹配（字段顺序可变）
            pattern = (
                r'"name"\s*:\s*"(?P<name>[^"]+)"[^}]*"matrix"\s*:\s*'
                r'(?P<matrix>"[^"]+"|\d+)[^}]*"title"\s*:\s*"(?P<title>[^"]+)"[^}]*'
                r'"mbti"\s*:\s*"(?P<mbti>[^"]+)"[^}]*"reason"\s*:\s*"(?P<reason>[^"]*(?:\\.[^"]*)*)"'
            )
            matches = list(re.finditer(pattern, result_text, re.DOTALL))

        for match in matches[:max_count]:
            name = match.group("name").strip()
            matrix_raw = match.group("matrix").strip()
            matrix = matrix_raw.strip('"')
            title = match.group("title").strip()
            mbti = match.group("mbti").strip()
            reason = match.group("reason").strip()

            # 清理转义字符
            reason = reason.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")

            titles.append(
                {
                    "name": name,
                    "matrix": matrix,
                    "title": title,
                    "mbti": mbti,
                    "reason": reason,
                }
            )

        logger.info(f"用户称号正则表达式提取成功，提取到 {len(titles)} 条有效用户称号")
        return titles

    except Exception as e:
        logger.error(f"用户称号正则表达式提取失败：{e}")
        return []


def extract_golden_quotes_with_regex(result_text: str, max_count: int) -> list[dict]:
    """
    使用正则表达式提取金句信息

    Args:
        result_text: 需要提取的文本
        max_count: 最大提取数量

    Returns:
        金句数据列表
    """
    try:
        quotes = []

        # 正则模式：匹配完整的金句对象
        pattern = r'\{\s*"content":\s*"([^"]*(?:\\.[^"]*)*)"\s*,\s*"sender":\s*"([^"]+)"\s*,\s*"reason":\s*"([^"]*(?:\\.[^"]*)*)"\s*\}'
        matches = re.findall(pattern, result_text, re.DOTALL)

        if not matches:
            # 尝试更宽松的匹配（字段顺序可变）
            pattern = r'"content":\s*"([^"]*(?:\\.[^"]*)*)"[^}]*"sender":\s*"([^"]+)"[^}]*"reason":\s*"([^"]*(?:\\.[^"]*)*)"'
            matches = re.findall(pattern, result_text, re.DOTALL)

        for match in matches[:max_count]:
            content = match[0].strip()
            sender = match[1].strip()
            reason = match[2].strip()

            # 清理转义字符
            content = (
                content.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")
            )
            reason = reason.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")

            quotes.append({"content": content, "sender": sender, "reason": reason})

        logger.info(f"金句正则表达式提取成功，提取到 {len(quotes)} 条有效金句")
        return quotes

    except Exception as e:
        logger.error(f"金句正则表达式提取失败：{e}")
        return []

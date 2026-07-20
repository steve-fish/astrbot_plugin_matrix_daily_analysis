"""
消息处理模块
负责群聊消息的获取、过滤和预处理
"""

from collections import defaultdict
from datetime import datetime, timedelta

from astrbot.api import logger

from ...src.models.data_models import EmojiStatistics, GroupStatistics, TokenUsage
from ...src.utils.time_utils import get_hour_from_timestamp
from ...src.visualization.activity_charts import ActivityVisualizer


class MessageHandler:
    """消息处理器"""

    _MAX_ANALYSIS_DAYS = 31

    def __init__(self, config_manager, bot_manager=None):
        self.config_manager = config_manager
        self.activity_visualizer = ActivityVisualizer()
        self.bot_manager = bot_manager

    async def set_bot_matrix_ids(self, bot_matrix_ids):
        """设置机器人 matrix 号（支持单个 matrix 号或 matrix 号列表）"""
        try:
            if self.bot_manager:
                # 确保传入的是列表，保持统一处理
                if isinstance(bot_matrix_ids, list):
                    self.bot_manager.set_bot_matrix_ids(bot_matrix_ids)
                elif bot_matrix_ids:
                    self.bot_manager.set_bot_matrix_ids([bot_matrix_ids])
            logger.info(f"设置机器人 matrix 号：{bot_matrix_ids}")
        except Exception as e:
            logger.error(f"设置机器人 matrix 号失败：{e}")

    def set_bot_manager(self, bot_manager):
        """设置 bot 管理器"""
        self.bot_manager = bot_manager

    def _extract_bot_matrix_id_from_instance(self, bot_instance):
        """从 bot 实例中提取 matrix 号（单个）"""
        if hasattr(bot_instance, "self_id") and bot_instance.self_id:
            return str(bot_instance.self_id)
        elif hasattr(bot_instance, "matrix") and bot_instance.matrix:
            return str(bot_instance.matrix)
        elif hasattr(bot_instance, "user_id") and bot_instance.user_id:
            return str(bot_instance.user_id)
        return None

    async def fetch_group_messages(
        self, bot_instance, group_id: str, days: int, platform_id: str | None = None
    ) -> list[dict]:
        """获取群聊消息记录"""
        try:
            # 验证参数
            if not group_id or not bot_instance:
                logger.error(f"群 {group_id} 参数无效")
                return []

            # 确保 bot_manager 有 matrix 号列表用于过滤
            if self.bot_manager and not self.bot_manager.has_bot_matrix_id():
                # 尝试从 bot_instance 提取 matrix 号并设置为列表
                bot_matrix_id = self._extract_bot_matrix_id_from_instance(bot_instance)
                if bot_matrix_id:
                    # 将单个 matrix 号转换为列表，保持统一处理
                    self.bot_manager.set_bot_matrix_ids([bot_matrix_id])

            try:
                normalized_days = int(days)
            except (TypeError, ValueError):
                normalized_days = 1
            normalized_days = max(1, min(normalized_days, self._MAX_ANALYSIS_DAYS))

            # 计算时间范围
            end_time = datetime.now()
            start_time = end_time - timedelta(days=normalized_days)

            logger.info(f"开始获取群 {group_id} 近 {normalized_days} 天的消息记录")
            logger.info(
                f"时间范围：{start_time.strftime('%Y-%m-%d %H:%M:%S')} 到 {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # 仅支持 Matrix 平台
            return await self._fetch_matrix_messages(
                bot_instance,
                group_id,
                normalized_days,
                start_time,
                end_time,
            )

        except Exception as e:
            logger.error(f"群 {group_id} 获取群聊消息记录失败：{e}", exc_info=True)
            return []

    async def _fetch_matrix_messages(
        self,
        bot_instance,
        group_id: str,
        days: int,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict]:
        """从 Matrix 平台获取群聊消息"""
        messages = []
        seen_event_ids = set()
        # Matrix use millisecond timestamp
        start_ts = int(start_time.timestamp() * 1000)
        end_ts = int(end_time.timestamp() * 1000)

        try:
            limit = self.config_manager.get_max_messages()
            logger.info(f"正在从 Matrix 获取消息，limit={limit}")
            filtered_prefixes = tuple(
                str(prefix).strip().casefold()
                for prefix in self.config_manager.get_history_filter_prefixes()
                if str(prefix or "").strip()
            )
            filtered_users = {
                str(user).strip().casefold()
                for user in self.config_manager.get_history_filter_users()
                if str(user or "").strip()
            }
            skip_bots = self.config_manager.should_skip_history_bots()

            # 获取群成员列表以填充昵称
            display_names = {}
            try:
                client = (
                    bot_instance.api if hasattr(bot_instance, "api") else bot_instance
                )
                if hasattr(client, "get_room_members"):
                    members_resp = await client.get_room_members(group_id)
                    member_events = (
                        members_resp.get("chunk", [])
                        if isinstance(members_resp, dict)
                        else []
                    )
                    if not isinstance(member_events, list):
                        member_events = []
                    for event in member_events:
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "m.room.member":
                            user_id = event.get("state_key")
                            content = event.get("content", {})
                            if not isinstance(content, dict):
                                continue
                            displayname = content.get("displayname")
                            if user_id and displayname:
                                display_names[user_id] = displayname
            except Exception as e:
                logger.warning(f"获取群成员列表失败，将无法显示昵称：{e}")

            # 使用 room_messages 分页获取历史消息（direction='b' 往后翻页）
            client = bot_instance.api if hasattr(bot_instance, "api") else bot_instance
            if hasattr(client, "room_messages"):
                from_token = None
                remaining = limit
                page_size = min(200, max(1, remaining))
                reached_start = False
                seen_tokens = set()

                while remaining > 0 and not reached_start:
                    response = await client.room_messages(
                        room_id=group_id,
                        limit=page_size,
                        direction="b",
                        from_token=from_token,
                    )
                    if not isinstance(response, dict):
                        logger.warning(
                            "Matrix history pagination returned invalid data; stopping"
                        )
                        break
                    chunk = response.get("chunk", [])
                    if not isinstance(chunk, list):
                        logger.warning(
                            "Matrix history pagination chunk is not a list; stopping"
                        )
                        break
                    if not chunk:
                        break

                    # Matrix 返回的是 chunk，包含 events（倒序：新->旧）
                    for event in chunk:
                        if not isinstance(event, dict):
                            continue
                        # 过滤非消息事件
                        if event.get("type") != "m.room.message":
                            continue

                        # 检查时间
                        try:
                            ts = int(event.get("origin_server_ts", 0))
                        except (TypeError, ValueError):
                            continue
                        if ts < start_ts:
                            reached_start = True
                            continue  # 太旧的消息
                        if ts > end_ts:
                            continue

                        content = event.get("content", {})
                        if not isinstance(content, dict):
                            continue
                        sender = str(event.get("sender", "") or "").strip()
                        if not sender:
                            continue
                        event_id = str(event.get("event_id", "") or "")
                        if event_id and event_id in seen_event_ids:
                            continue

                        # Apply global history filters before counting the limit so
                        # every analysis path receives the requested number of valid events.
                        if sender.casefold() in filtered_users:
                            continue
                        if (
                            skip_bots
                            and self.bot_manager
                            and self.bot_manager.should_filter_bot_message(sender)
                        ):
                            continue
                        body = str(content.get("body", "") or "")
                        normalized_body = body.lstrip().casefold()
                        if filtered_prefixes and normalized_body.startswith(
                            filtered_prefixes
                        ):
                            continue

                        # 获取昵称
                        nickname = display_names.get(sender, sender)

                        relation_type = ""
                        thread_root_id = ""
                        reply_event_id = ""
                        if self.config_manager.get_threading_enabled() and isinstance(
                            content, dict
                        ):
                            relates_to = content.get("m.relates_to", {})
                            if isinstance(relates_to, dict):
                                relation_type = str(
                                    relates_to.get("rel_type", "") or ""
                                )
                                thread_entry = relates_to.get("m.thread")
                                if isinstance(thread_entry, dict):
                                    thread_root_id = str(
                                        thread_entry.get("event_id", "") or ""
                                    )
                                    thread_reply = thread_entry.get("m.in_reply_to")
                                    if isinstance(thread_reply, dict):
                                        reply_event_id = str(
                                            thread_reply.get("event_id", "") or ""
                                        )
                                if not thread_root_id:
                                    thread_root_id = str(
                                        relates_to.get("event_id", "") or ""
                                    )
                                if not reply_event_id:
                                    reply = relates_to.get("m.in_reply_to")
                                    if isinstance(reply, dict):
                                        reply_event_id = str(
                                            reply.get("event_id", "") or ""
                                        )

                        # 转换消息格式
                        msg_dict = {
                            "time": ts / 1000,
                            "sender": {"user_id": sender, "nickname": nickname},
                            "message": [],
                            "event_id": event_id,
                            "relation_type": relation_type,
                            "thread_root_id": thread_root_id,
                            "reply_event_id": reply_event_id,
                        }

                        msg_type = content.get("msgtype")
                        if msg_type == "m.text":
                            msg_dict["message"].append(
                                {
                                    "type": "text",
                                    "data": {"text": content.get("body", "")},
                                }
                            )
                        elif msg_type == "m.image":
                            msg_dict["message"].append(
                                {
                                    "type": "image",
                                    "data": {"file": content.get("url", "")},
                                }
                            )
                        else:
                            # 其他类型作为文本处理
                            msg_dict["message"].append(
                                {
                                    "type": "text",
                                    "data": {
                                        "text": f"[{msg_type}] {content.get('body', '')}"
                                    },
                                }
                            )

                        if reply_event_id:
                            msg_dict["message"].append(
                                {
                                    "type": "reply",
                                    "data": {"id": reply_event_id},
                                }
                            )

                        messages.append(msg_dict)
                        if event_id:
                            seen_event_ids.add(event_id)
                        remaining -= 1
                        if remaining <= 0:
                            break

                    next_token = str(response.get("end", "") or "").strip()
                    if (
                        not next_token
                        or next_token == from_token
                        or next_token in seen_tokens
                    ):
                        break
                    seen_tokens.add(next_token)
                    from_token = next_token
                    page_size = min(200, max(1, remaining))

                # Matrix backward pagination returns newest events first. LLM prompts
                # need natural conversation order so replies follow their context.
                messages.sort(key=lambda message: message.get("time", 0))
                logger.info(f"Matrix 获取到 {len(messages)} 条有效消息")
                return messages
            else:
                logger.error("Bot 实例缺少 Matrix API 支持")
                return []

        except Exception as e:
            logger.error(f"Matrix 获取消息失败：{e}", exc_info=True)
            return []

    def calculate_statistics(self, messages: list[dict]) -> GroupStatistics:
        """计算基础统计数据"""
        total_chars = 0
        participants = set()
        hour_counts = defaultdict(int)
        emoji_statistics = EmojiStatistics()

        for msg in messages:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender", {})
            if not isinstance(sender, dict):
                continue
            sender_id = str(sender.get("user_id", "") or "").strip()
            if not sender_id:
                continue
            participants.add(sender_id)

            # 统计时间分布
            hour = get_hour_from_timestamp(msg.get("time", 0))
            hour_counts[hour] += 1

            # 处理消息内容
            message_items = msg.get("message", [])
            if not isinstance(message_items, list):
                continue
            for content in message_items:
                if not isinstance(content, dict):
                    continue
                data = content.get("data", {})
                if not isinstance(data, dict):
                    data = {}
                if content.get("type") == "text":
                    text = str(data.get("text", "") or "")
                    total_chars += len(text)
                elif content.get("type") == "face":
                    # matrix 基础表情
                    emoji_statistics.face_count += 1
                    face_id = data.get("id", "unknown")
                    emoji_statistics.face_details[f"face_{face_id}"] = (
                        emoji_statistics.face_details.get(f"face_{face_id}", 0) + 1
                    )
                elif content.get("type") == "mface":
                    # 动画表情/魔法表情
                    emoji_statistics.mface_count += 1
                    emoji_id = data.get("emoji_id", "unknown")
                    emoji_statistics.face_details[f"mface_{emoji_id}"] = (
                        emoji_statistics.face_details.get(f"mface_{emoji_id}", 0) + 1
                    )
                elif content.get("type") == "bface":
                    # 超级表情
                    emoji_statistics.bface_count += 1
                    emoji_id = data.get("p", "unknown")
                    emoji_statistics.face_details[f"bface_{emoji_id}"] = (
                        emoji_statistics.face_details.get(f"bface_{emoji_id}", 0) + 1
                    )
                elif content.get("type") == "sface":
                    # 小表情
                    emoji_statistics.sface_count += 1
                    emoji_id = data.get("id", "unknown")
                    emoji_statistics.face_details[f"sface_{emoji_id}"] = (
                        emoji_statistics.face_details.get(f"sface_{emoji_id}", 0) + 1
                    )
                elif content.get("type") == "image":
                    # 检查是否是动画表情（通过 summary 字段判断）
                    summary = str(data.get("summary", "") or "")
                    if "动画表情" in summary or "表情" in summary:
                        # 动画表情（以 image 形式发送）
                        emoji_statistics.mface_count += 1
                        file_name = data.get("file", "unknown")
                        emoji_statistics.face_details[f"animated_{file_name}"] = (
                            emoji_statistics.face_details.get(
                                f"animated_{file_name}", 0
                            )
                            + 1
                        )
                    else:
                        # 普通图片，不计入表情统计
                        pass
                elif (
                    content.get("type") in ["record", "video"]
                    and "emoji" in str(data).lower()
                ):
                    # 其他可能的表情类型
                    emoji_statistics.other_emoji_count += 1

        # 找出最活跃时段
        most_active_hour = (
            max(hour_counts.items(), key=lambda x: x[1])[0] if hour_counts else 0
        )
        most_active_period = (
            f"{most_active_hour:02d}:00-{(most_active_hour + 1) % 24:02d}:00"
        )

        # 生成活跃度可视化数据
        activity_visualization = (
            self.activity_visualizer.generate_activity_visualization(messages)
        )

        return GroupStatistics(
            message_count=len(messages),
            total_characters=total_chars,
            participant_count=len(participants),
            most_active_period=most_active_period,
            golden_quotes=[],
            emoji_count=emoji_statistics.total_emoji_count,  # 保持向后兼容
            emoji_statistics=emoji_statistics,
            activity_visualization=activity_visualization,
            token_usage=TokenUsage(),
        )

import os
import time
import uuid

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.errors import SlackClientError

# 在[Slack App]->[OAuth & Permissions]->[User OAuth Token] (xoxp-...)中找到令牌
SLACK_USER_TOKEN = "0000-0000000000000-0000000000000-0000000000000-00000000000000000000000000000000"

# channel_id在频道URL中找到，例如https://app.slack.com/client/T000000000/C0533PLC7V4 中的C0533PLC7V4
# 只需要打开你想要发送消息的频道，然后在地址栏中找到
# 必须填写正确的channel_id，否则无法使用
channel_id = "C0000000000"

# claude_id可以在 View App Details（查看应用详情） 中的 Member ID （成员 ID） 中找到
# 本ID是Slack内部使用的ID，不是Slack用户名或bot ID，不要混淆
# 本ID是用来标识Claude回复的消息的，如果不使用本ID，不太容易区分Claude的回复和我们用来发送消息的Bot的回复
# 并且，如果假设在消息列中有实时在线的用户的回复，有ID就可以辨认出来
# 因此也建议用一个专用的Slack工作区，不要和其他用户使用的混在一起
claude_id = "U000000000"

# 观察中发现如果在Claude回复后还处在Typing状态， 那么这时候发送消息， Claude的反应会受影响
# 角色扮演的影响非常大， 所以默认开启
# 如果不需要角色扮演等上下文相关或不需要精确的回复，可以关闭
# !注意！经过测试Claude同时Typing的回复数量有限，如果超过一定数量，Claude会暂缓其他Typing并等待正在进行的回复完成后再继续
wait_til_message_finish = True

# 使用机器人TOKEN实例化Web客户端
client = WebClient(token=SLACK_USER_TOKEN)
# 会话列表用于记录会话ID和消息Time Stamp(ts) ID，用于辨认响应消息，
# 会话锁以及防止重复发送消息导致消息错乱, 只要Claude开始回复(指Typing)，就会解锁会话，实际时间应该在不到1秒左右
# 短的等待时间换来消息列表对齐是值得的
sessions = {}
sessions_history = {"": []}
queue_message = {}

# 最大重试次数，如果响应时间超过3秒，则更新消息重试，重试次数超过最大次数，则返回未响应
max_retries = 5
message_receiving_interval = 1
def send_message_to_channel(channel_id:str=channel_id, message_text:str = "",session_id:str = "g01"):
    try:
        is_new_session = False
        uniq_ID = message_text + str(uuid.uuid1())[:8]
        if not queue_message.get(session_id):
                queue_message[session_id] = [uniq_ID]
        else:
            queue_message.get(session_id).append(uniq_ID)

        # 如果会话ID在会话列表中，则等待会话解锁
        while queue_message.get(session_id) and uniq_ID in queue_message.get(session_id) and queue_message.get(session_id).index(uniq_ID) > 0:
            print(f"等待会话解锁... {session_id} 1 秒后重试...")
            time.sleep(1)
        if not queue_message.get(session_id) or uniq_ID not  in queue_message.get(session_id):
            return
        # 如果会话ID不在会话列表中，则发送新消息，并记录会话ID和time stamp, 否则发送消息列回复
        if session_id not in sessions:
            is_new_session = True
            result = send_message(channel_id,message_text)
            if not result['ok']:
                # 解锁会话
                pop_message(session_id,uniq_ID)
                return result['error']
            sessions[session_id] = result['ts']
        else:
            result = send_message(channel_id,message_text,sessions.get(session_id))
        if not result['ok']:
            # 解锁会话
            pop_message(session_id,uniq_ID)
            return result['error']
        # 记录time stamp用于后续辨认响应消息
        ts = result['ts']
        # 初始化响应为_Typing…_，表示正在等待响应
        response = '_Typing…_'
        # 记录响应开始时间,重试次数
        start_time = time.time()
        reties = 1
        last_message = ""
        # 如果响应以_Typing…_结尾，则继续等待响应
        while response.strip().endswith('_Typing…_'):
            replies = receive_message(channel_id=channel_id,ts=sessions.get(session_id),oldest=ts)
            # 如果replies['ok']为False或消息列表长度小于等于1，则表示没有响应
            if not replies:
                # 解锁会话
                pop_message(session_id,uniq_ID)
                raise SlackApiError("未收到Claude响应，请重试。")
            if not replies['ok'] or (time.time() - start_time > 10 and len(replies['messages']) <= 1):
                if replies['error'] == 'ratelimited':
                    print(f"被限速了， 将在5秒后重试...")
                    time.sleep(5)
                    continue
                # 如果重试次数超过{max_retries}次，则返回未响应
                # 否则更新消息从而触发@Claude的响应
                if reties >= max_retries:
                    # 解锁会话
                    pop_message(session_id,uniq_ID)
                    return f'以重试{max_retries}次，未收到Claude响应，请重试。'
                else:
                    # 如果重试次数未超过{max_retries}次，则更新消息从而触发@Claude的响应
                    print(f"重试 {reties} 次... 最大重试次数: {max_retries} 次")
                    update_message(channel_id, ts, message_text)
                    start_time = time.time()
                    reties += 1
                    continue
            if len(replies['messages']) <= 1:
                continue
            for index, message in enumerate(replies['messages'][1:],start=1):
                if message['user'] != claude_id:
                    continue
                response = message['text']
                if index < len(replies['messages']) - 1 and any(warn_tip in replies['messages'][index + 1]['text'] for warn_tip in["*Please note:*", "Oops! Claude was un"]):
                    delete_message(channel_id, replies['messages'][-1]['ts'])
                pop_message(session_id, uniq_ID, not is_new_session and not wait_til_message_finish)
                break
            _display_stream_data(response.removesuffix('_Typing…_').removesuffix("\n") + "...")
            time.sleep(message_receiving_interval)

        # print(f"Message sent to channel {channel_id}...\nResponds:\n{response}")
        # 解锁会话
        pop_message(session_id,uniq_ID,is_new_session or wait_til_message_finish)
        return response

    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return return_err(e.response['error'])
    # except nonetype error
    except SlackClientError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return return_err(e)
    except TypeError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return return_err(" 消息发送失败，请检查USER_TOKEN等参数是否正确，或重试。")
    except Exception as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return return_err(e)


def send_message_to_channel_API_mode(channel_id: str = channel_id, message_text: str = "", session_id: str = "g01"):
    try:
        is_new_session = False
        uniq_ID = message_text + str(uuid.uuid1())[:8]
        if not queue_message.get(session_id):
            queue_message[session_id] = [uniq_ID]
        else:
            queue_message.get(session_id).append(uniq_ID)

        # 如果会话ID在会话列表中，则等待会话解锁
        while queue_message.get(session_id) and uniq_ID in queue_message.get(session_id) and queue_message.get(
                session_id).index(uniq_ID) > 0:
            print(f"等待会话解锁... {session_id} 1 秒后重试...")
            time.sleep(1)
        if not queue_message.get(session_id) or uniq_ID not in queue_message.get(session_id):
            return
        # 如果会话ID不在会话列表中，则发送新消息，并记录会话ID和time stamp, 否则发送消息列回复
        ts = None
        if session_id in sessions:
            is_new_session = True
            sessions_history[session_id].append({"role": "user", "content": message_text})

            delete_message(channel_id, sessions[session_id])
            result = send_message(channel_id, message_text)
            if not result['ok']:
                # 解锁会话
                pop_message(session_id, uniq_ID)
                return result['error']
            sessions[session_id] = result['ts']
            ts = result['ts']
        # 初始化响应为_Typing…_，表示正在等待响应
        response = '_Typing…_'
        # 记录响应开始时间,重试次数
        start_time = time.time()
        reties = 1
        last_message = ""
        # 如果响应以_Typing…_结尾，则继续等待响应
        while response.strip().endswith('_Typing…_'):
            replies = receive_message(channel_id=channel_id, ts=sessions.get(session_id), oldest=ts)
            # 如果replies['ok']为False或消息列表长度小于等于1，则表示没有响应
            if not replies:
                # 解锁会话
                pop_message(session_id, uniq_ID)
                raise SlackApiError("未收到Claude响应，请重试。")
            if not replies['ok'] or (time.time() - start_time > 10 and len(replies['messages']) <= 1):
                if replies['error'] == 'ratelimited':
                    print(f"被限速了， 将在5秒后重试...")
                    time.sleep(5)
                    continue
                # 如果重试次数超过{max_retries}次，则返回未响应
                # 否则更新消息从而触发@Claude的响应
                if reties >= max_retries:
                    # 解锁会话
                    pop_message(session_id, uniq_ID)
                    return f'以重试{max_retries}次，未收到Claude响应，请重试。'
                else:
                    # 如果重试次数未超过{max_retries}次，则更新消息从而触发@Claude的响应
                    print(f"重试 {reties} 次... 最大重试次数: {max_retries} 次")
                    update_message(channel_id, ts, message_text)
                    start_time = time.time()
                    reties += 1
                    continue
            if len(replies['messages']) <= 1:
                continue
            for index, message in enumerate(replies['messages'][1:], start=1):
                if message['user'] != claude_id:
                    continue
                response = message['text']
                if index < len(replies['messages']) - 1 and any(
                        warn_tip in replies['messages'][index + 1]['text'] for warn_tip in
                        ["*Please note:*", "Oops! Claude was un"]):
                    delete_message(channel_id, replies['messages'][-1]['ts'])
                pop_message(session_id, uniq_ID, not is_new_session and not wait_til_message_finish)
                break
            _display_stream_data(response.removesuffix('_Typing…_').removesuffix("\n") + "...")
            time.sleep(message_receiving_interval)

        # print(f"Message sent to channel {channel_id}...\nResponds:\n{response}")
        # 解锁会话
        pop_message(session_id, uniq_ID, is_new_session or wait_til_message_finish)
        sessions_history[session_id].append({"role": "assistant", "content": response})
        return response

    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id, uniq_ID)
        return return_err(e.response['error'])
    # except nonetype error
    except SlackClientError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id, uniq_ID)
        return return_err(e)
    except TypeError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id, uniq_ID)
        return return_err(" 消息发送失败，请检查USER_TOKEN等参数是否正确，或重试。")
    except Exception as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id, uniq_ID)
        return return_err(e)


### 普通方法区 ###

def pop_message(session_id:str="",uniq_ID:str="",bool_expression:bool=True):
    if queue_message.get(session_id) and uniq_ID in queue_message.get(session_id) and bool_expression:
        pop_index = queue_message.get(session_id).index(uniq_ID)
        queue_message.get(session_id).pop(pop_index)

def switch_message_mode():
    global wait_til_message_finish
    queue_message.clear()
    wait_til_message_finish = not wait_til_message_finish
    return wait_til_message_finish

def get_message_mode():
    return wait_til_message_finish


### Slack API 方法区 ###

# 发送@Claude的消息
# 如果thread_ts为空，则发送新消息
# 如果thread_ts不为空，则发送消息列回复
def send_message(channel_id,text:str,tread_ts:str = ''):
    try:
        # 使用Web客户端调用chat.postMessage方法
        result = client.chat_postMessage(
            channel=channel_id,
            text=f'<@{claude_id}>{text}',
            thread_ts = tread_ts
        )
        return result
    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")


# 获取消息列
def receive_message(channel_id,ts,oldest):
    try:
        # 使用Web客户端调用conversations.replies方法
        result = client.conversations_replies(  ts = ts,
                                                channel = channel_id,
                                                oldest = oldest)
        return result
    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")


# 更新消息, 用于触发@Claude的响应
def update_message(channel_id,ts,text:str):
    try:
        # 使用Web客户端调用chat.update方法
        result = client.chat_update(
            channel=channel_id,
            ts=ts,
            text=f'<@{claude_id}>{text}'
        )
        return result
    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")


def delete_message(channel_id,ts):
    try:
        # 使用Web客户端调用chat.delete方法
        result = client.chat_delete(
            channel=channel_id,
            ts=ts,
            as_user=True
        )
        return result
    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")

# for test use only#########

pronoun_presets = {'default': {'user': '你', 'claude': 'Claude'}}
current_preset = ""

def _default_data_file():
    default_file = {
        "USER_TOKEN": "",
        "channel_id": "",
        "claude_id": "",
        "sessions": {},
        "pronouns": {'default': {'user': '你', 'claude': 'Claude'}}
    }
    return default_file



def _get_filepath():
    import pathlib
    # 获取用户AppData文件夹，用于在其中创建data.txt文件
    appdata_folder = pathlib.Path(os.environ['APPDATA'])
    # 创建一个名为"Claude_CMD"的子文件夹
    Claude_folder = appdata_folder / "Claude_CMD"
    Claude_folder.mkdir(exist_ok=True)
    file_path = Claude_folder / 'data.json'
    return file_path

def get_txt_files_in_directory():
    import sys
    # 获取当前文件的绝对路径
    # 获取 exe 文件所在目录的绝对路径
    exe_path = sys.argv[0]
    exe_dir = os.path.dirname(exe_path)
    presets_folder_path = os.path.join(exe_dir, 'presets')
    # 检查文件夹是否存在
    if os.path.exists(presets_folder_path):
        # 获取文件夹内的所有文件和子文件夹
        items = os.listdir(presets_folder_path)
        # 过滤出所有txt文件
        txt_files = [str(item).removesuffix('.txt') for item in items if item.endswith('.txt')]
        # 如果文件夹为空或没有txt文件
        if not txt_files:
            return return_err("不存在或目录为空")
        else:
            return txt_files
    else:
        return return_err("不存在或目录为空")

def _load_preset(preset_name):
    if preset_name not in get_txt_files_in_directory():
        return return_err(f"预设 {preset_name} 不存在!")
    current_file_path = os.path.abspath(__file__)
    current_directory = os.path.dirname(current_file_path)
    presets_folder_path = os.path.join(current_directory, 'presets', preset_name+'.txt')

    with open(presets_folder_path, 'r', encoding='utf-8') as f:
        return f.read()


def _load_json_file():
    file_content_template = _default_data_file()
    try:
        file_path = _get_filepath()
        if not file_path.exists():
            raise FileNotFoundError
        with open(file_path, "r") as f:
            file_content_template = json.load(f)
            if not file_content_template.get("sessions"):
                file_content_template["sessions"] = {}
            global SLACK_USER_TOKEN, channel_id, claude_id,client, pronoun_presets
            SLACK_USER_TOKEN = file_content_template["USER_TOKEN"]
            channel_id = file_content_template["channel_id"]
            claude_id = file_content_template["claude_id"]
            client = WebClient(token=SLACK_USER_TOKEN)
            pronoun_presets = file_content_template["pronouns"] if isinstance(file_content_template.get("pronouns"), dict) \
                else _default_data_file().get("pronouns")
    except FileNotFoundError as e:
        print(f"错误: {e.__cause__}, 未找到配置文件，将创建新的配置文件")
        return _save_json_file(_input_config(_default_data_file()))
    except Exception as e:
        print(f"错误:{e}, 配置文件损坏，将创建新的配置文件")
        return _save_json_file(_input_config(_default_data_file()))
    return file_content_template

def _save_json_file(file_content_save, notice=False):
    try:
        file_path = _get_filepath()
        with open(file_path, "w") as f:
            if notice:
                print("保存中...")
            json.dump(file_content_save, f, indent=4)
            return file_content_save
    except Exception as e:
        print(e)
        return _default_data_file()

def _input_config(input_content=None):
    if not input_content:
        input_content = _load_json_file()
    global SLACK_USER_TOKEN, channel_id,claude_id,client
    print("请填写以下信息，直接回车则不填写, 可后续通过[config]修改，\n请确保你填写的信息是有效的,在填写后会自动保存至配置文件中。\n")
    token = input("请输入你的Slack User OAuth Token:\n    ")
    input_content["USER_TOKEN"] = token if token else SLACK_USER_TOKEN
    input_content["channel_id"] = input("请输入你的Slack频道ID:\n    ") if token else channel_id
    input_content["claude_id"] = input("请输入Claude的Slack用户ID:\n    ") if token else claude_id
    SLACK_USER_TOKEN = input_content["USER_TOKEN"]
    channel_id = input_content["channel_id"]
    claude_id = input_content["claude_id"]
    client = WebClient(token=SLACK_USER_TOKEN)
    os.system('cls')
    _save_json_file(input_content)
    return input_content

def _display_history(ts, show=True, with_fix=False):
    if not ts:
        return
    try:
        message_history = receive_message(channel_id, ts, ts)
        if not message_history or not message_history['ok']:
            raise Exception("读取失败")
        message_return = []
        if show:
            print("历史会话:")
        for message in message_history["messages"]:
            this_id = message["user"]
            pronouns = _get_pronouns()
            name_prefix = f"\033[32m{pronouns.get('user')}:\n    " if this_id != claude_id else f"\033[33m{pronouns.get('claude')}:\n   "
            name_suffix = "\033[0m"
            dialoge_line = ""
            if show:
                print(name_prefix + message["text"].removeprefix(f"<@{claude_id}>") + name_suffix)
            if with_fix:
                dialoge_line += name_prefix + message["text"].removeprefix(f"<@{claude_id}>") + name_suffix
            else:
                dialoge_line += name_prefix[8:] + message["text"].removeprefix(f"<@{claude_id}>")
            message_return.append(dialoge_line)
        return message_return
    except SlackApiError as e:
        print(e.response["error"])
        return "读取失败"
    except Exception as e:
        print(e)
        return "读取失败"


stream_data_history = ""
def _display_stream_data(input_text):
    os.system('cls')
    print(stream_data_history + f"\n\033[33m{_get_pronouns().get('claude')}\033[0m:" + f"\n   \033[33m{input_text.removesuffix('_Typing…_')}\033[0m")

def _set_stream_data(session_id, input_text):
    global stream_data_history
    stream_data_history += _get_title(session_id, False) + '\n'
    if sessions.get(session_id):
        stream_data_history += '\n'.join(_display_history(sessions[session_id], False, True)) + '\n'
    stream_data_history += f"\033[32m{_get_pronouns().get('user')}\033[0m:\n    " + input_text

def _clear_stream_data():
    global stream_data_history
    stream_data_history = ""

# 颜色相关
def _hash_string(string):
    import random
    random.seed(string)
    return hash(string)

def _map_hash_to_color(hash_value):
    colors = [31, 32, 33, 34, 35, 36, 37, 91, 92, 93, 94, 95, 96, 97]
    return colors[hash_value % len(colors)]

def _get_colored(text):
    color_code = _map_hash_to_color(_hash_string(text))
    return f"\033[{color_code}m{text}\033[0m"

# 以下是ANSI转义序列的说明
"""
在命令行（CMD）中，可以使用ANSI转义序列来设置文本的前景色和背景色。以下是在CMD中可用的ASCII颜色代码：
前景色（文字颜色）：
黑色：30
红色：31
绿色：32
黄色：33
蓝色：34
品红（洋红）：35
青色：36
白色：37
背景色：
黑色：40
红色：41
绿色：42
黄色：43
蓝色：44
品红（洋红）：45
青色：46
白色：47
要在CMD中设置文本颜色，可以使用以下格式的ANSI转义序列：
\033[<前景色>;<背景色>m
例如，要将文本颜色设置为红色（31）并将背景色设置为白色（47），可以使用以下代码：
\033[31;47m
"""


# 关键字方法区 #

def save(input_session_id, input_file_content, *args):
    """
    保存当前会话ID,当前时间,当前预设到配置文件中
    如果没有输入自定义会话ID,则保存当前会话ID
    """
    try:
        session_id_save = input_session_id
        if args:
            session_id_save = str(args[0])
        if not sessions.get(input_session_id):
            _save_json_file(input_file_content, notice=True)
            print("配置文件已保存！")
            return return_err("当前会话还没有消息，无法保存! 请先发送一条消息！")
        if input_file_content["sessions"].get(input_session_id):
            input_file_content["sessions"].pop(input_session_id, None)
        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
        input_file_content["sessions"][session_id_save] = {"ts": sessions.get(input_session_id), "preset": current_preset, 'time': formatted_time}
        sessions[session_id_save] = sessions.get(input_session_id)
        _save_json_file(input_file_content, notice=True)
        print(_get_title(session_id_save))
        _display_history(input_file_content["sessions"].get(session_id_save).get("ts"))
        return {"session_id": session_id_save, "file_content": input_file_content,
                "message": f"保存成功！ID: {session_id_save}\n下次就可以用这个ID加载会话了！"}
    except Exception as e:
        return return_err(f"保存失败！Error: {e}")

def load(*args):
    """
    加载会话
    不输入任何参数则加载配置文件
    """
    if not len(args) > 2:
        _load_json_file()
        return return_err("以加载配置文件！")
    session_id_load = args[2]
    try:
        input_file_content = _load_json_file()
        session_id_ts = input_file_content["sessions"].get(session_id_load.lower())
        if not session_id_ts:
            return return_err("会话ID不存在!")
        else:
            session_id_ts = session_id_ts.get("ts")
            sessions[session_id_load] = input_file_content["sessions"].get(session_id_load.lower()).get("ts")
            global current_preset
            current_preset = input_file_content["sessions"].get(session_id_load.lower()).get("preset")
            print(f"会话 ID: {session_id_load} 加载成功!")
            print(_get_title(session_id_load))
            _display_history(session_id_ts)
        return {"session_id": session_id_load}
    except ValueError as e:
        _load_json_file()
        return

def history(*args):
    """
    显示历史会话列表
    格式为: [ID] [预设] [时间]
    """
    history_list = _load_json_file().get("sessions")
    if not history_list or len(history_list) == 0:
        return return_err("没有保存的历史会话!")
    print(_get_title(session_id))
    print(f"\033[32m使用 [load 会话 ID] 来回到某个历史会话\033[0m\n\033[32m会话 ID 列表\033[0m: ")
    print(f"    {_get_colored('[ID]'):<25}{_get_colored('[预设]'):<23}{_get_colored('[时间]'):<25}")
    for key, value in history_list.items():
        preset = "N/A"
        history_time = "N/A"
        if value and value.get("preset"):
            preset = value.get("preset")
        if value and value.get("time"):
            history_time = value.get("time")

        print(f"    {_get_colored(key):<25}{_get_colored(preset):<25}{_get_colored(history_time):<25}")
    return

def load_preset(input_session_id, input_file_content, *args):
    """
    用于加载预设
    不输入任何参数则列出所有预设
    预设文件需为.txt文件
    """
    print(_get_title(input_session_id))
    intent_preset = str(args[0]) if args else None
    if intent_preset:
        preset_list = get_txt_files_in_directory()
        if isinstance(preset_list, dict) and preset_list.get("error"):
            return preset_list
        preset_content = _load_preset(intent_preset)
        if isinstance(preset_content, dict) and preset_content.get("error"):
            return preset_content
        print(preset_content)
        if input(f"确认切换到 {intent_preset} 预设吗, 这将开启新对话? (按 回车 确认, 'N' 取消)").lower() != 'n':
            new_id = str(uuid.uuid1())[:8]
            print(_get_title(new_id))
            print("    ", preset_content)
            input_file_content["sessions"][new_id] = {"preset": intent_preset}
            input_file_content.pop(input_session_id, None)
            return {"text": preset_content, "session_id": new_id, "file_content": input_file_content, "current_preset": intent_preset}
        else:
            return return_err("切换预设已取消!")
    else:
        print(f"\033[31m当前预设\033[0m: {_get_colored(current_preset) if current_preset else '无'}\n")
        print(f"\033[96m使用 [preset 预设名称] 来切换预设\033[0m")
        print(f"\033[31m预设列表\033[0m: ")
        for preset_name in get_txt_files_in_directory():
            print(f"    {_get_colored(preset_name)}")
        return

def display_nested_dict(nested_dict, indent=0):
    nested_result = ""
    for key, value in nested_dict.items():
        nested_result += "\n"
        if isinstance(value, dict):
            nested_result += f"{' ' * indent}{_get_colored(key)}:"
            nested_result += display_nested_dict(value, indent + 4)
        else:
            nested_result += f"{' ' * indent}{_get_colored(key)}: {_get_colored(value)}"
    return nested_result

def set_nested_dict(nested_dict, command_list):
    key = command_list[0]
    value = command_list[1]
    try:
        if len(command_list) == 2:
            if key in ["delete", "del"]:
                if value not in nested_dict:
                    return return_err(f"参数 {value} 不存在!")
                nested_dict.pop(value, None)
                print(f"删除参数 {value} 成功!")
                return nested_dict
            if key in nested_dict:
                if isinstance(nested_dict[key].get(value), dict):
                    print(display_nested_dict(nested_dict[key].get(value), 4))
                    return nested_dict
                nested_dict[key] = command_list[1]
                return nested_dict
            raise KeyError
        else:
            nested_dict[key] = set_nested_dict(nested_dict[key], command_list[1:])
        return nested_dict
    except KeyError as e:
        input_command = input(f"没有这个参数 {key} , 按 回车 新建,'N' 取消")
        if input_command.lower() == 'n':
            return return_err("新建参数已取消!")
        if len(command_list) < 3:
            nested_dict[key] = value
            return nested_dict
        elif len(command_list) > 2:
            nested_dict[key] = {}
            return set_nested_dict(nested_dict, command_list)
    except TypeError as e:
        input_command = input(f"参数 {key} 不是一个字典! 要新建一个吗? (按 回车 新建,'N' 取消)")
        if input_command.lower() == 'n':
            return return_err("新建参数已取消!")
        nested_dict[key] = {}
        return set_nested_dict(nested_dict, command_list)

def config(input_session_id, input_file_content, *args):
    """
    配置指令, 用于配置会话的一些参数
    输入 config 来查看当前会话的配置
    输入 config [参数] [值] 来修改参数的值
    输入 config sessions 来查看已保存的所有会话
    输入 config sessions [会话ID] [值] 来修改某个会话的ID
    输入 config sessions delete [会话ID] 来删除某个会话
    输入 config pronouns 来查看已保存的所有代词
    比如 config pronouns xxx user 我 就可以把 xxx 预设下的用户的代词设置为我
    """
    try:
        if not args:
            print(_get_title(input_session_id))
            print(f"\033[32m{config.__doc__}\033[0m")
            print(f"\033[32m参数列表\033[0m: ")
            user_token = str(input_file_content.get("USER_TOKEN"))
            secret_token = user_token[:8] + str('*' * (len(user_token) - 8)) + user_token[len(user_token) - 8:]
            print(f"    {'USER_TOKEN':15}: {_get_colored(secret_token)}")
            print(f"    {'channel_id':15}: {_get_colored(input_file_content.get('channel_id'))}")
            print(f"    {'claude_id':15}: {_get_colored(input_file_content.get('claude_id'))}")
            if not isinstance(input_file_content.get("pronouns"), dict) or len(input_file_content.get("pronouns")) == 0:
                input_file_content["pronouns"] = _default_data_file().get("pronouns")
                _save_json_file(input_file_content, False)
            print(f"\033[32m当前会话的代词[只显示前5个, 使用 preset 查看全部]\033[0m: ")
            if len(input_file_content.get("pronouns")) > 0:
                print("    {:<24}{:<23}{:<22}".format(_get_colored('预设'), "\033[32m你的名字\033[0m",
                                                      "\033[33mClaude的名字\033[0m"))
                if 'default' not in input_file_content.get("pronouns"):
                    input_file_content["pronouns"]["default"] = _default_data_file().get("pronouns").get("default")
                defaul_pronouns = input_file_content.get("pronouns").get("default")
                print("    {:<24}{:<23}{:<23}".format(_get_colored('默认'),
                                                      f"\033[32m{defaul_pronouns.get('user')}\033[0m",
                                                      f"\033[33m{defaul_pronouns.get('claude')}\033[0m"))
                for index, (key, value) in enumerate(input_file_content["pronouns"].items()):
                    if index == 5:
                        break
                    if key.lower() == "default":
                        continue
                    print("    {:<25}{:<25}{:<25}".format(_get_colored(key), _get_colored(value.get('user')),
                                                          _get_colored(value.get('claude'))))
        else:
            if len(args) < 1:
                return return_err("参数不足!")
            if args[0] not in input_file_content and args[0] not in ["user_token"]:
                return return_err(f"没有找到 {args[0]} 参数!")
            if args[0] == "sessions":
                if len(args) == 1:
                    print("")
                    history()
                if len(args) == 3 and args[1] in ["delete", "del"]:
                    if input_file_content["sessions"].get(args[2]):
                        input_file_content["sessions"].pop(args[2])
                        _save_json_file(input_file_content, notice=True)
                        _load_json_file()
                        print(f"会话 ID: {args[2]} 删除成功! 已保存.")
                        return
                    return return_err("会话ID不存在!")
            if args[0] in input_file_content.keys():
                if not isinstance(input_file_content[args[0]], dict):
                    if len(args) < 2:
                        return {"message": f"当前 {args[0]} 参数: {_get_colored(input_file_content[args[0]])}"}
                    input_file_content[args[0]] = args[1]
                    _save_json_file(input_file_content, notice=True)
                    _load_json_file()
                    print(f"参数 {args[0]} 已更新为: {args[1]}")
                else:
                    if len(args) < 2:
                        return {
                            "message": f"当前 {args[0]} 参数: {display_nested_dict(input_file_content[args[0]], 4)}"}
                    if len(args) > 1:
                        config_update_result = set_nested_dict(input_file_content, args)
                        if config_update_result.get("error"):
                            return config_update_result
                        input_file_content = config_update_result
                        _save_json_file(input_file_content, notice=True)
                        _load_json_file()
                        if any(key_word in ['del', 'delete'] for key_word in args):
                            return {"message": f"参数 {'.'.join(args[:-2])}.{args[-1]} 已删除! 已保存!"}
                        return {"message": f"参数 {'.'.join(args[:-1])} 以更新为: {args[-1]}, 已保存!"}
                return
            else:
                return return_err("参数错误!")
    except Exception as e:
        return return_err(f"参数错误! {e}")

def refresh(input_session_id, *args):
    """
    刷新并重新加载会话
    """
    print(_get_title(input_session_id))
    if sessions.get(input_session_id):
        _display_history(sessions[input_session_id])

def help_command(input_session_id, input_file_content, *args):
    """
    帮助指令, 用于显示指令列表或指定指令的说明
    输入 help [指令] 来查看指令的说明
    """
    print(_get_title(input_session_id))
    if args and args[0] in commands_mapping:
        print(f"指令 \033[{color_codes.get(str(args[0]),36)}m{str(args[0]).capitalize()}\033[0m 的说明：")
        print(f"\033[{color_codes.get(str(args[0]),36)}m{commands_mapping[str(args[0])].__doc__}\033[0m")
    else:
        print(f"指令列表：")
        for cmd in _get_cmd_list():
            print("    ", cmd)

# 请根据代码详细描述本指令的功能
def clear(*args):
    """
    清屏
    清除的消息记录并开始新的会话
    """
    print("\033[33m" + "Clearing the screen..." + "\033[0m")
    session_id_new = str(uuid.uuid1())[:8]
    print(_get_title(session_id_new))
    return {"session_id": session_id_new}

def quit_app(*args):
    """
    直接退出程序
    """
    exit(0)

# 用于显示命令列表
commands_mapping = {"quit": quit_app,
                    "clear": clear,
                    "load": load,
                    "save": save,
                    "config": config,
                    "history": history,
                    "refresh": refresh,
                    "help": help_command,
                    "preset": load_preset,
                    }
commands = {"preset": "'文件名' 加载预设,预设文件放在一样目录下的\\presets文件夹",
            "save": "‘自订ID/空’ 保存会话",
            "load": "‘会话ID’ 加载会话",
            "refresh": "刷新输出界面",
            "config": "配置Token环境",
            "history": "查看历史会话",
            "clear": "重置会话",
            "quit": "退出",
            "help": "'指令名' 查看详细帮助"}
color_codes = {
    "quit": 31,   # 红色
    "clear": 32,  # 绿色
    "config": 36,  # 青色
    "save": 34,   # 蓝色
    "load": 35,   # 洋红色
    "history": 33,  # 黄色
    "help": 37,    # 白色
    "refresh": 32,  # 绿色
    "preset": 96  # 紫罗兰
    }

def return_err(err_text):
    return {"error": err_text}

def _get_cmd_list(only_keys=False):
    cmd_list = []
    for k, v in commands.items():
        cmd_list.append(f"<\033[{color_codes.get(k,36)}m{k.capitalize()}\033[0m" + (f" [\033[{color_codes.get(k,37)}m{v}\033[0m]> " if not only_keys else "> "))
    return cmd_list

def _get_title(session_id, clear_screen=True):
    if clear_screen:
        os.system('cls')
    first_line = f"\033[92m\033[1m" + "# CLAUDE 青春版 #" + "\033[0m"
    second_line = "".join(_get_cmd_list(only_keys=True))
    last_line = f"\033[34m当前会话 ID\033[0m: {_get_colored(session_id)} "
    if current_preset:
        last_line += f"\033[34m当前预设\033[0m: {_get_colored(current_preset)} "
        last_line += f"\033[34m名称代词\033[0m: [\033[32m用户\033[0m: \033[32m{_get_pronouns()['user']}\033[0m] " \
                     f"[\033[33m克劳德\033[0m: \033[33m{_get_pronouns()['claude']}\033[0m]"
    return "\n".join([first_line, second_line, last_line]) + "\n"

def _get_pronouns():
    pronouns = pronoun_presets.get(current_preset, pronoun_presets['default'] if pronoun_presets.get('default')
                                   else _default_data_file()['pronouns']['default'])
    if not pronouns.get('user'):
        pronouns['user'] = _default_data_file()['pronouns']['default']['user']
    if not pronouns.get('claude'):
        pronouns['claude'] = _default_data_file()['pronouns']['default']['claude']
    return pronouns


if __name__ == "__main__":
    import json
    os.system('cls')
    message_receiving_interval = 0.75
    file_content = _load_json_file()
    session_id = str(uuid.uuid1())[:8]

    print(_get_title(session_id))
    while True:
        # 标记input text为绿色
        text = input(f"\033[32m{_get_pronouns()['user']}\033[0m:\n    ")

        if not text:
            print("无效输入!")
            continue
        input_keyword = text.lower().split()[0]
        input_args = text.split()[1:]
        if input_keyword in commands_mapping:
            move_next = True
            result = commands_mapping[input_keyword](session_id, file_content, *input_args)
            if result:
                if result.get("session_id"):
                    session_id = result.get("session_id")
                if result.get("file_content"):
                    file_content = result.get("file_content")
                if result.get("error"):
                    print(result.get("error"))
                if result.get("message"):
                    print(result.get("message"))
                if result.get("current_preset"):
                    current_preset = result.get("current_preset")
                if result.get("text"):
                    text = result.get("text")
                    move_next = False
            if move_next:
                continue
        print(f"\033[33m{_get_pronouns()['claude']}\033[0m:")
        _set_stream_data(session_id, text)
        response = send_message_to_channel(channel_id=file_content.get('channel_id'), message_text=text, session_id=session_id)
        session = file_content["sessions"].get(session_id)
        if response:
            if isinstance(response, dict) and response.get("error"):
                print(response.get("error"))
                _clear_stream_data()
                continue
            is_session_exist = session and session.get('ts') and sessions.get(session_id)
            if not is_session_exist:
                formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
                if not session:
                    file_content["sessions"][session_id] = {}
                file_content["sessions"][session_id]['ts'] = sessions.get(session_id)
                file_content["sessions"][session_id]['time'] = formatted_time
                _save_json_file(file_content)
            _display_stream_data(response)
        _clear_stream_data()

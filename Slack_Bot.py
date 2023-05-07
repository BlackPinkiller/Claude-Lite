import os
import time
import uuid
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.errors import SlackClientError
from slack_sdk.errors import SlackRequestError

# 在[Slack App]->[OAuth & Permissions]->[User OAuth Token] (xoxp-...)中找到令牌
SLACK_USER_TOKEN = "xoxp-5125030454129-5097862626087-5112805795925-64797889d79cb32eb0042cf49ef07e88"

# channel_id在频道URL中找到，例如https://app.slack.com/client/T000000000/C0533PLC7V4 中的C0533PLC7V4
# 只需要打开你想要发送消息的频道，然后在地址栏中找到
# 必须填写正确的channel_id，否则无法使用
channel_id = "C0533PLC7V4"

# claude_id可以在 View App Details（查看应用详情） 中的 Member ID （成员 ID） 中找到
# 本ID是Slack内部使用的ID，不是Slack用户名或bot ID，不要混淆
# 本ID是用来标识Claude回复的消息的，如果不使用本ID，不太容易区分Claude的回复和我们用来发送消息的Bot的回复
# 并且，如果假设在消息列中有实时在线的用户的回复，有ID就可以辨认出来
# 因此也建议用一个专用的Slack工作区，不要和其他用户使用的混在一起
claude_id = "U0537DSKGF7"

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
        poped_item = None
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
            for index,message in enumerate(replies['messages'][1:],start=1):
                if message['user'] != claude_id:
                    continue
                response = message['text']
                if index < len(replies['messages']) - 1 and any(warn_tip in replies['messages'][index + 1]['text'] for warn_tip in["*Please note:*", "Oops! Claude was un"]):
                    client.chat_delete(
                        channel=channel_id,
                        ts=replies['messages'][-1]['ts'],
                        as_user=True
                        )
                pop_message(session_id,uniq_ID, not is_new_session and not wait_til_message_finish)
                break
            time.sleep(message_receiving_interval)
            
        # print(f"Message sent to channel {channel_id}...\nResponds:\n{response}")
        # 解锁会话
        pop_message(session_id,uniq_ID,is_new_session or wait_til_message_finish)
        return response
    except SlackApiError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return str(e.response['error'])
    # except nonetype error
    except SlackClientError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return str(e)
    except TypeError as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return str(e)
    except Exception as e:
        print(f"Error posting message to channel {channel_id}: {e}")
        # 解锁会话
        pop_message(session_id,uniq_ID)
        return str(e)


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



# for test use only

def _get_filepath():
    import pathlib
    # 获取用户AppData文件夹，用于在其中创建data.txt文件
    appdata_folder = pathlib.Path(os.environ['APPDATA'])
    # 创建一个名为"MyAppName"的子文件夹（替换为你的应用程序名称）
    Claude_folder = appdata_folder / "Claude_CMD"
    Claude_folder.mkdir(exist_ok=True)
    file_path = Claude_folder / 'data.json'
    return file_path

def _load_json_file():
    try:
        file_path = _get_filepath()
        if not file_path.exists():
            with open(file_path, "a+") as f:
                file_content = _input_config(file_content)
                json.dump(file_content, f, indent=4)
        with open(file_path, "r") as f:
            file_content = json.load(f)
            if not file_content.get("sessions"):
                file_content["sessions"] = {}
            global SLACK_USER_TOKEN,channel_id,claude_id,client
            SLACK_USER_TOKEN = file_content["USER_TOKEN"]
            channel_id = file_content["channel_id"]
            claude_id = file_content["claude_id"]
            client = WebClient(token=SLACK_USER_TOKEN)
    except Exception as e:
        file_content = {   
        "USER_TOKEN": "",
        "channel_id": "",
        "claude_id": "",
        "sessions": {}
        }
        _input_config(file_content)
        return file_content
    return file_content

def _save_json_file(file_content,notice=False):
    try:
        file_path = _get_filepath()
        with open(file_path, "w") as f:
            if notice:
                print("保存中...")
            json.dump(file_content, f, indent=4)
    except Exception as e:
        print(e)

def _input_config(file_content = None):
    if not file_content:
        file_content = _load_json_file()
    global SLACK_USER_TOKEN,channel_id,claude_id,client
    print("请填写以下信息，直接回车则使用默认值，\n请确保你填写的信息是有效的,在填写后会自动保存至配置文件中。\n")
    token = input("请输入你的Slack User OAuth Token:\n    ")
    file_content["USER_TOKEN"] = token if token else SLACK_USER_TOKEN
    file_content["channel_id"] = input("请输入你的Slack频道ID:\n    ") if token else channel_id
    file_content["claude_id"] = input("请输入Claude的Slack用户ID:\n    ") if token else claude_id
    SLACK_USER_TOKEN = file_content["USER_TOKEN"]
    channel_id = file_content["channel_id"]
    claude_id = file_content["claude_id"]
    client = WebClient(token=SLACK_USER_TOKEN)
    os.system('cls')
    _save_json_file(file_content)
    return file_content
        
def _display_history(ts):
    try:
        message_history = receive_message(channel_id,ts,ts)
        if not message_history or not message_history['ok']:
            raise Exception("读取失败")
        message_return = []
        print("历史会话:")
        for message in message_history["messages"]:
            this_id = message["user"]
            name_prefix = f"\033[32m你:\n    " if this_id != claude_id else f"\033[33mClaude:\n    "
            name_suffix = "\033[0m"
            print(name_prefix + message["text"].removeprefix(f"<@{claude_id}>") + name_suffix)
            message_return.append(name_prefix[8:] + message["text"].removeprefix(f"<@{claude_id}>"))
        return message_return
    except SlackApiError as e:
        print(e.response["error"])
        return "读取失败"
    except SlackRequestError as e:
        print(e.response["error"])
        return "读取失败"
    except SlackClientError as e:
        print(e.response["error"])
        return "读取失败"
    except Exception as e:
        print(e)
        return "读取失败"
            

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

# 用于显示命令列表
commands = {"Quit": "退出","Clear": "重置会话","Config": "配置Token环境","Save": "‘自订ID/空’ 保存会话","Load": "‘会话ID’ 加载会话","refresh":"刷新输出界面","History": "查看历史会话","Help": "帮助"}
color_codes = {
    "Quit": 31,   # 红色
    "Clear": 32,  # 绿色
    "Config": 36, # 青色
    "Save": 34,   # 蓝色
    "Load": 35,   # 洋红色
    "History": 33, # 黄色
    "Help": 37,    # 白色
    "refresh": 32 # 绿色
    }

def _get_cmd_list(only_keys=False):
    cmd_list = []
    for k,v in commands.items():
        cmd_list.append(f"<\033[{color_codes.get(k,36)}m{k}\033[0m" + (f" [\033[{color_codes.get(k,37)}m{v}\033[0m]> " if not only_keys else "> "))
    return cmd_list

def _get_title(session_id):
    os.system('cls')
    first_line = f"\033[92m\033[1m" + "# CLAUDE 青春版 #" + "\033[0m"
    second_line = "".join(_get_cmd_list(only_keys=True))
    last_line = f"\033[34m当前会话 ID\033[0m: {_get_colored(session_id)} "
    return "\n".join([first_line,second_line,last_line])





if __name__ == "__main__":
    import json
    os.system('cls')
    message_receiving_interval = 0.1
    file_content = _load_json_file()
    session_id = str(uuid.uuid1())[:8]

    print(_get_title(session_id))
    while True:
        # 标记input text为绿色
        text = input(f"\033[32m你\033[0m:\n    ")
        if not text:
            print("无效输入!")
            continue
        if text.lower().strip() == "quit":
            exit()
        elif text.lower().strip() == "clear":
            print("\033[33m" + "Clearing the screen..." + "\033[0m")
            os.system('cls')
            session_id = str(uuid.uuid1())[:8]
            print(_get_title(session_id))
            continue
        elif "load" in text.lower().strip():
            session_id_load = str(text.lower()).removeprefix("load").strip()
            if not session_id_load:
                print("不能输入空会话ID!")
                continue
            try:
                file_content = _load_json_file()
                session_id_ts =  file_content["sessions"].get(session_id_load.lower())
                if not session_id_ts:
                    print("会话ID不存在!")
                    continue
                else:
                    session_id = session_id_load
                    sessions[session_id] = file_content["sessions"].get(session_id_load.lower())
                    print(f"会话 ID: {session_id} 加载成功!")
                    print(_get_title(session_id))
                    _display_history(session_id_ts)
                continue
            except ValueError as e:
                _load_json_file(file_content)
                continue
        elif "save" in text.lower().strip():
            try:
                session_id_save = str(text.lower()).removeprefix("save").strip()
                if not session_id_save:
                    session_id_save = session_id
                if not sessions.get(session_id):
                    print("当前会话还没有消息，无法保存!")
                    continue
                if file_content["sessions"].get(session_id):
                    file_content["sessions"].pop(session_id,None)
                file_content["sessions"][session_id_save] = sessions.get(session_id)
                sessions[session_id_save] = sessions.get(session_id)
                session_id = session_id_save
                _save_json_file(file_content, notice=True)
                print(f"保存成功！ID: {session_id_save}\n下次就可以用这个ID加载会话了！")
                print(_get_title(session_id))
                _display_history(file_content["sessions"].get(session_id))
                continue
            except Exception as e:
                print(f"保存失败！Error: {e}")
            continue
        elif text.lower().strip() == "config":
            print(_get_title(session_id))
            file_content = _input_config()
            print(claude_id)
            continue
        elif text.lower().strip() == "history":
            history = _load_json_file().get("sessions")
            if not history or len(history) == 0:
                print("没有保存的历史会话!")
                continue
            os.system('cls')
            print(_get_title(session_id))
            print(f"\033[32m使用 [load 会话 ID] 来回到某个历史会话\033[0m\n\033[32m会话 ID 列表\033[0m: ")
            for key,value in history.items():
                print(f"    {_get_colored(key)}")
            continue
        # this is help command section, it's use for helping information
        elif text.lower().strip() == "help":
            os.system('cls')
            print(_get_title(session_id))
            print(f"指令列表：")
            for cmd in _get_cmd_list():
                print("    ",cmd)
            continue
        elif text.lower().strip() == "refresh":
            print(_get_title(session_id))
            continue
        print(f"\033[33mClaude\033[0m:")
        response = send_message_to_channel(channel_id=file_content.get('channel_id'),message_text=text,session_id=session_id)
        if response and not file_content["sessions"].get(session_id) and sessions.get(session_id):
            file_content["sessions"][session_id] = sessions.get(session_id)
            _save_json_file(file_content)
        print(f"   \033[33m{response}\033[0m")

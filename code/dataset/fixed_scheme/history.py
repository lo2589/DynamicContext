"""Pinned schema sketch retained as documentation, not executable Python.

history={
  "0": {"system": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  "1": {"user": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "think": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "assistant": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  "2": {"user": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "think": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "assistant": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  "3": {"user": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "think": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "assistant": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  "4": {"user": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "think": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "assistant": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  "5": {"user": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "think": {"content":"...",range:[[[a,b],compat_rate,compat_way]]},
        "assistant": {"content":"...",range:[[[a,b],compat_rate,compat_way]]}},
  ...
}
### 钉死的格式，每role只准占据一行，range可能有多个，对应不同压缩器
### 【a,b】表示生效范围，对话诞生必须带，生命周期结束必须更新
###可能还有别的元素比如“goal”，不定长元素，存储原始真实信息

life_cycle={
    "system":[1,None],
    "user":['born',None],
    "think":["born","born"],
    "goal":["born","born"+"m"],
}
state_table={
  "0": {"system": "<state of the ele>"},
  "1": {"user": "<state of the ele>",
        "think": "<state of the ele>",
        "assistant": "<state of the ele>"},
  "2": {"user": "<state of the ele>",
        "think": "<state of the ele>",
        "assistant": "<state of the ele>"},
  "3": {"user": "<state of the ele>",
        "think": "<state of the ele>",
        "assistant": "<state of the ele>"},
  "4": {"user": "<state of the ele>",
        "think": "<state of the ele>",
        "assistant": "<state of the ele>"},
  "5": {"user": "<state of the ele>",
        "think": "<state of the ele>",
        "assistant": "<state of the ele>"},
  ...
}
context={#opanai格式

}

"""

<div align="center">

# 🔇 QQ 防刷屏

<i>🚫 叽里咕噜说啥呢？太长不看！</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

---

## 📖 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的防刷屏插件。当群员发送的消息超过指定长度或图片数量阈值时，自动将消息转为合并转发并撤回原消息，保持群聊整洁。

---

## ✨ 功能特性

- 📏 **长度检测** - 自动检测消息长度，超过阈值则触发合并转发
- 🖼️ **图片检测** - 支持按图片数量阈值触发转发
- 👥 **群成员处理** - 对群员的长消息进行转发与撤回
- 🛡️ **权限保护** - 操作前自动检查机器人撤回权限，无权限则跳过避免异常

---

## 📖 使用方法

在 AstrBot 插件管理中启用即可。

### 💡 示例

> **用户发送超长消息（超过 300 字）**
>
> → 机器人将其转为合并转发并撤回原消息

---

## ⚙️ 配置说明

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和介绍。

---

## 🔄 版本历史

详见 [CHANGELOG.md](./CHANGELOG.md)

---

## ❤️ 支持

- [AstrBot 帮助文档](https://astrbot.app)
- 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_anti_flood)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>

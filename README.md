---
title: Binance Futures Trading Bot
emoji: 📈
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# Binance Futures Trading Bot

Binance Futures Testnet trading bot with **Web UI Dashboard** and **Web Terminal** access.

## 🚀 Quick Start (Local)

```bash
pip install -r requirements.txt
python app.py
```

Then open http://localhost:7860

## 🌐 Hugging Face Spaces

This bot is designed to run on Hugging Face Spaces using the Docker SDK.

### Modes

| Mode | Description |
|------|-------------|
| **🖥️ Web UI Dashboard** | Full trading dashboard with positions, orders, and trade form |
| **⌨️ Terminal Access** | Interactive CLI via browser-based xterm.js terminal |

## 🔐 Environment Variables

| Variable | Description |
|----------|-------------|
| `BINANCE_TESTNET_API_KEY` | Binance Futures Testnet API key |
| `BINANCE_TESTNET_API_SECRET` | Binance Futures Testnet API secret |

Get credentials from [testnet.binancefuture.com](https://testnet.binancefuture.com/)

## 📁 Project Structure

```
trading_bot/
├── app.py              ← Main entry point (Landing + Terminal server)
├── dashboard.py        ← Dashboard API server
├── cli.py              ← CLI-based trading interface
├── bot/                ← Core trading logic
│   ├── client.py         Binance API client
│   ├── orders.py         Order placement logic
│   ├── portfolio.py      Portfolio tracking
│   ├── validators.py     Input validation
│   └── logging_config.py Logging setup
├── ui/                 ← Frontend files
│   ├── index.html
│   ├── style.css
│   └── app.js
└── logs/               ← Log files
```

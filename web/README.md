# OpenRag Web Frontend

React 18 + TypeScript frontend application for OpenRag system.

## Setup

1. Install dependencies:
```bash
npm install
```

2. Create `.env` file:
```bash
cp .env.example .env
```

3. Start development server:
```bash
npm run dev
```

The application will be available at http://localhost:3000

## Build

```bash
npm run build
```

## Features

- 用户注册 / 登录（JWT）
- 项目空间与文件树：上传、目录、预览、任务状态
- 语义检索与分块预览
- 「权限」页：汇总当前账号在各工作区的读写来源（角色 / 单独授权）
- 管理员：服务令牌（`/service/v1` 机读接口）、角色与工作区授权（「管理权限」）
- 设置与个人资料；界面文案支持 i18n（`src/i18n`）

## Tech Stack

- React 18
- TypeScript
- Vite
- Ant Design
- React Router
- Axios

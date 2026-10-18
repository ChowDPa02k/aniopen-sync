import "@ant-design/v5-patch-for-react-19";
import React, { lazy, Suspense, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  ConfigProvider,
  Input,
  Modal,
  Space,
  Typography,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import { api, clearToken, setToken } from "./api";
import siteIcon from "./assets/favicon.ico";
import "./style.css";

const Dashboard = lazy(() =>
  import("./Dashboard").then((module) => ({ default: module.Dashboard })),
);

function Root() {
  const [hasAccess, setHasAccess] = useState(false),
    [locked, setLocked] = useState(true);
  const [booting, setBooting] = useState(true),
    [checking, setChecking] = useState(false);
  const [token, setInputToken] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const lost = () => {
      setLocked(true);
      setError("会话已失效，请重新验证令牌；未保存的配置会保留");
    };
    window.addEventListener("aniopen-unauthorized", lost);
    (async () => {
      try {
        const health = await api("/health", { token: "", quiet401: true });
        const stored = sessionStorage.getItem("aniopen-token") || "";
        if (stored || !health.authentication_required) {
          await api("/status", { token: stored, quiet401: true });
          if (active) {
            setToken(stored);
            setHasAccess(true);
            setLocked(false);
          }
        }
      } catch (err) {
        if (active) setError((err as Error).message);
      } finally {
        if (active) setBooting(false);
      }
    })();
    return () => {
      active = false;
      window.removeEventListener("aniopen-unauthorized", lost);
    };
  }, []);
  const login = async () => {
    setChecking(true);
    try {
      await api("/status", { token, quiet401: true });
      setToken(token);
      setInputToken("");
      setHasAccess(true);
      setLocked(false);
      setError("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setChecking(false);
    }
  };
  const logout = () => {
    clearToken();
    setHasAccess(false);
    setLocked(true);
    setError("");
  };
  const form = (
    <>
      <Typography.Paragraph type="secondary">
        首次启动令牌见容器启动日志；也可执行 docker compose exec aniopen-sync
        aniopen-sync token 查看。
      </Typography.Paragraph>
      {error && <Alert message={error} type="error" />}
      <Input.Password
        aria-label="访问令牌"
        style={{ margin: "20px 0" }}
        value={token}
        onChange={(event) => setInputToken(event.target.value)}
        onPressEnter={login}
        placeholder="访问令牌"
        autoComplete="off"
      />
      <Button
        aria-label="进入控制台"
        block
        type="primary"
        loading={checking}
        disabled={booting}
        onClick={login}
      >
        进入控制台
      </Button>
    </>
  );
  return (
    <>
      {hasAccess ? (
        <Suspense fallback={<Card loading />}>
          <Dashboard onLogout={logout} />
        </Suspense>
      ) : (
        <div className="login">
          <Card
            loading={booting}
            title={
              <Space>
                <img className="site-icon" src={siteIcon} alt="" width={24} height={24} />
                ANiOpen Sync
              </Space>
            }
          >
            {form}
          </Card>
        </div>
      )}
      <Modal
        open={hasAccess && locked}
        title="重新验证访问令牌"
        footer={null}
        closable={false}
        maskClosable={false}
        keyboard={false}
      >
        {form}
      </Modal>
    </>
  );
}

const theme = {
  token: {
    colorPrimary: "#2563eb",
    borderRadius: 8,
    fontFamily:
      'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
  },
  components: { Card: { boxShadowTertiary: "0 2px 12px rgba(18,38,63,.03)" } },
};
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={theme}>
      <AntApp>
        <Root />
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);

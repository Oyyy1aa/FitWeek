import { useEffect, useState } from "react";

const sections = [
  "总览",
  "个人资料",
  "每周计划",
  "Agent 草稿",
  "训练进度",
  "记忆管理",
  "ICS 导出",
  "运行记录",
  "设置",
];

type User = { display_name: string };
type Plan = {
  id: string;
  week_start: string;
  status: string;
  estimated_total_minutes: number;
  sessions: { id: string }[];
};

type DashboardState = {
  user: User | null;
  plans: Plan[];
  loading: boolean;
  error: boolean;
};

const initialState: DashboardState = {
  user: null,
  plans: [],
  loading: true,
  error: false,
};

async function requestJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`FitWeek API request failed with ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export default function App() {
  const [activeSection, setActiveSection] = useState(sections[0]);
  const [dashboard, setDashboard] = useState(initialState);

  useEffect(() => {
    let current = true;
    void Promise.all([requestJson<User>("/api/v1/users/me"), requestJson<Plan[]>("/api/v1/plans")])
      .then(([user, plans]) => {
        if (current) setDashboard({ user, plans, loading: false, error: false });
      })
      .catch(() => {
        if (current) setDashboard({ user: null, plans: [], loading: false, error: true });
      });
    return () => {
      current = false;
    };
  }, []);

  const currentPlan = dashboard.plans[0];
  const heading = dashboard.user
    ? `${dashboard.user.display_name}，本周训练计划`
    : activeSection === "总览"
      ? "本周训练计划"
      : activeSection;

  return (
    <main className="app-shell">
      <aside aria-label="FitWeek 主导航">
        <div className="brand">FitWeek</div>
        <p className="tagline">把每周训练安排得清楚、稳妥。</p>
        <nav>
          {sections.map((section) => (
            <button
              className={activeSection === section ? "active" : ""}
              key={section}
              onClick={() => setActiveSection(section)}
              type="button"
            >
              {section}
            </button>
          ))}
        </nav>
      </aside>
      <section className="content">
        <header>
          <div>
            <p className="eyebrow">{activeSection}</p>
            <h1>{heading}</h1>
          </div>
          <span className="mode">本地单用户模式</span>
        </header>
        <div className="notice" role="note">
          当前版本采用本地单用户模式，尚未实现账户认证，不应直接部署到开放公网。
        </div>
        {dashboard.loading ? <p className="status">正在同步训练数据…</p> : null}
        {dashboard.error ? (
          <div className="error" role="alert">
            <strong>训练数据暂时不可用</strong>
            <span>请确认 FitWeek API 已启动；页面不会把失败伪装成成功。</span>
          </div>
        ) : null}
        {!dashboard.loading && !dashboard.error ? (
          <section className="cards" aria-label="训练摘要">
            <article>
              <p>本周计划</p>
              <strong>{currentPlan ? currentPlan.week_start : "尚未创建"}</strong>
              <span>{currentPlan ? `状态：${currentPlan.status}` : "可通过 API 生成或创建计划"}</span>
            </article>
            <article>
              <p>预计训练量</p>
              <strong>{currentPlan ? `${currentPlan.estimated_total_minutes} 分钟` : "0 分钟"}</strong>
              <span>来自 API 的真实计划数据</span>
            </article>
            <article>
              <p>训练安排</p>
              <strong>{currentPlan ? `${currentPlan.sessions.length} 次训练` : "0 次训练"}</strong>
              <span>正式状态仍由 MySQL 作为事实源</span>
            </article>
          </section>
        ) : null}
      </section>
    </main>
  );
}

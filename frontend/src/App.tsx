import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CalendarDays,
  Check,
  ChevronRight,
  Copy,
  FolderClosed,
  Image,
  Layers,
  ListFilter,
  LoaderCircle,
  Menu,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  SlidersHorizontal,
  SwatchBook,
  X,
  type LucideIcon,
} from "lucide-react";
import * as Tooltip from "@radix-ui/react-tooltip";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { Button } from "./components/ui/button";
import { api, demoRuns, modeLabel, modes, plan, stageLabel, type Run } from "./data";
import { LocaleSwitch, formatDateTime, useI18n } from "./i18n";
import CreatePage from "./CreatePage";
import { BrandCreativeStudio, type StudioTarget } from "./BrandCreativeStudio";
import OperationsPage from "./OperationsPage";
import ShopsPage from "./ShopsPage";
import { AnimatedSelection, PolishedAsset, TabPanel } from "./components/polish";
import { IconButton, Status } from "./components/console-controls";
import { go } from "./navigation";
import {
  RunMediaGallery,
  RunMediaStrip,
  TaskMediaPanel,
  type MediaPreviewOptions,
} from "./components/run-media";
import type { RunMedia } from "./data";
function route() {
  const path = location.hash.slice(1) || "/tasks";
  return path === "/research" ? "/product-research" : path;
}
export default function App() {
  const { locale, t } = useI18n();
  const reduced = useReducedMotion() ?? false;
  const settingsRef = useRef<HTMLDivElement>(null);
  const settingsButtonRef = useRef<HTMLButtonElement>(null);
  const [preview, setPreview] = useState<{
    run: Run;
    media: RunMedia;
    trigger: HTMLElement;
    options?: MediaPreviewOptions;
  } | null>(null);
  const [studio, setStudio] = useState<StudioTarget | null>(null);
  const [path, setPath] = useState(route),
    [demo, setDemo] = useState(
      () => new URLSearchParams(location.search).get("mode") !== "live",
    );
  const [runs, setRuns] = useState<Run[]>(() => (demo ? demoRuns(locale) : [])),
    [detail, setDetail] = useState<Run | null>(null),
    [loading, setLoading] = useState(!demo),
    [detailLoading, setDetailLoading] = useState(false),
    [error, setError] = useState("");
  const [revision, setRevision] = useState(0),
    [filter, setFilter] = useState("all"),
    [query, setQuery] = useState(""),
    [modeFilter, setModeFilter] = useState("all"),
    [toast, setToast] = useState(""),
    [busy, setBusy] = useState(false),
    [mobileNav, setMobileNav] = useState(false),
    [detailTab, setDetailTab] = useState("overview"),
    [settingsOpen, setSettingsOpen] = useState(false);
  const operation = [
    "product-research",
    "competitor-research",
    "memory",
    "calendar",
    "data",
  ].find((name) => path === `/${name}`);
  const isShops = path.startsWith("/shops"),
    isNew = path === "/new",
    isAssets = path === "/assets",
    selectedId = path.startsWith("/tasks/")
      ? decodeURIComponent(path.slice(7))
      : null,
    current =
      detail?.id === selectedId
        ? detail
        : runs.find((r) => r.id === selectedId);
  const viewKey = operation
    ? `operation:${operation}:${demo}`
    : isShops
      ? `shops:${demo}`
      : isNew
        ? `new:${demo}`
        : selectedId
          ? `task:${selectedId}:${demo}`
          : isAssets
            ? `assets:${demo}`
            : `tasks:${demo}`;
  useEffect(() => {
    const update = () => {
      setPath(route());
      setMobileNav(false);
      setSettingsOpen(false);
      setError("");
      setPreview(null);
      setStudio(null);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    if (!settingsOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!settingsRef.current?.contains(event.target as Node))
        setSettingsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setSettingsOpen(false);
      settingsButtonRef.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [settingsOpen]);
  const filters = [
    ["all", t("All", "全部")],
    ["running", t("Active", "进行中")],
    ["waiting_review", t("Needs review", "待审核")],
    ["completed", t("Completed", "已完成")],
  ];
  useEffect(() => {
    const labels: Record<string, string> = {
      "product-research": t("Product research", "产品研究"),
      "competitor-research": t("Competitor research", "竞品研究"),
      memory: t("Memory", "记忆库"),
      calendar: t("Publishing calendar", "发布排期"),
      data: t("Data", "数据"),
    };
    document.title = `crispy · ${
      operation
        ? labels[operation]
        : isShops
          ? t("Shops & credentials", "店铺与凭证")
          : isNew
            ? t("New task", "新建任务")
            : selectedId
              ? t("Task detail", "任务详情")
              : isAssets
                ? t("Product assets", "产品素材")
                : t("Tasks", "任务")
    } — the marketing workbench`;
  }, [isNew, selectedId, isAssets, isShops, operation, t]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 3500);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (demo || isShops || operation) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<Run[]>("/runs", { signal: controller.signal })
      .then(setRuns)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [demo, revision, isShops, operation]);
  useEffect(() => {
    if (demo || isShops || operation) return;
    let queued: number | undefined;
    const bump = () => {
      if (!document.hidden) setRevision((r) => r + 1);
    };
    const scheduleBump = () => {
      if (document.hidden || queued) return;
      queued = window.setTimeout(() => {
        queued = undefined;
        bump();
      }, 250);
    };
    const source =
      typeof EventSource === "undefined"
        ? null
        : new EventSource("/api/runs/stream");
    source?.addEventListener("runs_changed", scheduleBump);
    const onVisible = () => {
      if (!document.hidden) bump();
    };
    document.addEventListener("visibilitychange", onVisible);
    // Safety net if the stream is unavailable or the connection has dropped.
    const fallback = window.setInterval(() => {
      if (!document.hidden) bump();
    }, 30000);
    return () => {
      source?.removeEventListener("runs_changed", scheduleBump);
      source?.close();
      if (queued) window.clearTimeout(queued);
      document.removeEventListener("visibilitychange", onVisible);
      window.clearInterval(fallback);
    };
  }, [demo, isShops, operation]);
  useEffect(() => {
    setDetail(null);
    setDetailTab("overview");
  }, [selectedId, demo]);
  useEffect(() => {
    if (demo || !selectedId) {
      setDetailLoading(false);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    api<Run>(`/runs/${encodeURIComponent(selectedId)}`, {
      signal: controller.signal,
    })
      .then(setDetail)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedId, demo, revision]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        document.getElementById("task-search")?.focus();
      }
      if (e.key === "Escape") setMobileNav(false);
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      document.querySelector<HTMLElement>(".page-title")?.focus(),
    );
    return () => cancelAnimationFrame(frame);
  }, [path]);
  function switchMode(value: string) {
    setPreview(null);
    setSettingsOpen(false);
    const next = value === "demo";
    setDemo(next);
    setRuns(next ? demoRuns(locale) : []);
    setDetail(null);
    setError("");
    history.replaceState(
      null,
      "",
      `${location.pathname}?mode=${value}${location.hash}`,
    );
    if (isShops)
      go(path.startsWith("/shops/shared") ? "/shops/shared" : "/shops");
    else if (selectedId || isNew) go("/tasks");
  }
  function navigate(next: string) {
    setFilter("all");
    setQuery("");
    setModeFilter("all");
    go(next);
    setMobileNav(false);
  }
  useEffect(() => {
    if (demo) setRuns(demoRuns(locale));
  }, [locale, demo]);
  const visible = runs.filter(
    (r) =>
      (filter === "all" ||
        r.status === filter ||
        (filter === "running" && r.status === "queued")) &&
      (modeFilter === "all" || r.pipeline_mode === modeFilter) &&
      `${r.name || ""} ${r.product_code} ${r.id}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  async function review(action: "advance" | "reject") {
    if (!current) return;
    setBusy(true);
    try {
      if (demo) {
        const stages = plan(current),
          next = stages[stages.indexOf(current.current_stage || "") + 1];
        setRuns((rows) =>
          rows.map((r) =>
            r.id === current.id
              ? {
                  ...r,
                  status:
                    action === "reject"
                      ? "queued"
                      : next
                        ? "running"
                        : "completed",
                  current_stage:
                    action === "reject"
                      ? r.current_stage
                      : next || r.current_stage,
                  updated_at: new Date().toISOString(),
                }
              : r,
          ),
        );
        setToast(
          action === "advance"
            ? t("Demo: approved", "演示：已通过")
            : t("Demo: requeued", "演示：已重新排队"),
        );
      } else {
        await api(`/runs/${encodeURIComponent(current.id)}/${action}`, {
          method: "POST",
          body: JSON.stringify({ notes: "" }),
        });
        setRevision((r) => r + 1);
        setToast(
          action === "advance"
            ? t("Approved", "已通过")
            : t("Rejected", "已驳回"),
        );
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Tooltip.Provider delayDuration={300}>
      <div className="console">
        <div className="mobile-nav-toggle">
          <IconButton
            icon={mobileNav ? X : Menu}
            label={mobileNav ? t("Close navigation", "关闭导航") : t("Open navigation", "打开导航")}
            expanded={mobileNav}
            onClick={() => setMobileNav((v) => !v)}
          />
        </div>
        <aside className={`navigation ${mobileNav ? "navigation-open" : ""}`}>
          <a
            href="#/tasks"
            className="workspace-brand"
            onClick={() => navigate("/tasks")}
          >
            <span className="brand-mark">
              <SwatchBook size={24} strokeWidth={1.8} />
            </span>
            <span>
              <strong>crispy</strong>
            </span>
          </a>
          <div className="nav-context">
            <FolderClosed size={16} />
            {t("Workspace", "工作空间")}
          </div>
          <div className="nav-section">{t("Marketing work", "营销工作")}</div>
          <nav aria-label={t("Main navigation", "主导航")}>
            {(
              [
                ["/tasks", t("Tasks", "任务"), Layers],
                ["/assets", t("Product assets", "产品素材"), Image],
              ] as [string, string, LucideIcon][]
            ).map(([target, label, Icon]) => (
              <a
                href={`#${target}`}
                className={`nav-link ${(target === "/tasks" && !isAssets && !isShops && !operation) || (target === "/assets" && isAssets) ? "active" : ""}`}
                aria-current={
                  (target === "/tasks" &&
                    !isAssets &&
                    !isShops &&
                    !operation) ||
                  (target === "/assets" && isAssets)
                    ? "page"
                    : undefined
                }
                key={target}
                onClick={() => navigate(target)}
              >
                <Icon size={16} />
                {label}
              </a>
            ))}
            <div className="nav-section separated">
              {t("Insights & operations", "洞察与运营")}
            </div>
            {(
              [
                ["product-research", t("Product research", "产品研究"), Search],
                [
                  "competitor-research",
                  t("Competitor research", "竞品研究"),
                  ListFilter,
                ],
                ["memory", t("Memory", "记忆库"), BookOpen],
                [
                  "calendar",
                  t("Publishing calendar", "发布排期"),
                  CalendarDays,
                ],
                ["data", t("Data", "数据"), Activity],
              ] as [string, string, LucideIcon][]
            ).map(([id, label, Icon]) => (
              <a
                className={`nav-link ${operation === id ? "active" : ""}`}
                aria-current={operation === id ? "page" : undefined}
                onClick={() => navigate(`/${id}`)}
                href={`#/${id}`}
                key={id}
              >
                <Icon size={16} />
                {label}
              </a>
            ))}
          </nav>
          <div className="nav-bottom">
            <a
              className={`nav-link ${isShops ? "active" : ""}`}
              aria-current={isShops ? "page" : undefined}
              href="#/shops"
              onClick={() => navigate("/shops")}
            >
              <Settings2 size={16} />
              {t("Shops & credentials", "店铺与凭证")}
            </a>
            <div className="sidebar-settings" ref={settingsRef}>
              <button
                ref={settingsButtonRef}
                type="button"
                className="sidebar-settings-toggle"
                aria-expanded={settingsOpen}
                aria-controls="workspace-settings"
                onClick={() => setSettingsOpen((open) => !open)}
              >
                <span>
                  <SlidersHorizontal size={16} />
                  {t("Settings", "设置")}
                </span>
                <span className="sidebar-settings-value">
                  {demo ? t("Demo", "演示") : t("Live", "本地")}
                </span>
                <motion.span
                  className="sidebar-settings-chevron"
                  animate={{ rotate: settingsOpen ? 90 : 0 }}
                  transition={
                    reduced
                      ? { duration: 0 }
                      : { duration: 0.18, ease: [0.22, 1, 0.36, 1] }
                  }
                >
                  <ChevronRight size={13} />
                </motion.span>
              </button>
              <AnimatePresence initial={false}>
                {settingsOpen && (
                  <motion.div
                    id="workspace-settings"
                    className="sidebar-settings-panel"
                    role="group"
                    aria-label={t("Settings", "设置")}
                    initial={
                      reduced ? false : { opacity: 0, y: 8, scale: 0.98 }
                    }
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={
                      reduced
                        ? { opacity: 0 }
                        : { opacity: 0, y: 6, scale: 0.98 }
                    }
                    transition={{
                      duration: reduced ? 0 : 0.18,
                      ease: [0.22, 1, 0.36, 1],
                    }}
                  >
                    <div className="sidebar-settings-body">
                      <div className="sidebar-setting">
                        <span>{t("Language", "语言")}</span>
                        <LocaleSwitch />
                      </div>
                      <label className="sidebar-setting">
                        <span>{t("Data mode", "数据模式")}</span>
                        <select
                          value={demo ? "demo" : "live"}
                          onChange={(e) => switchMode(e.target.value)}
                        >
                          <option value="demo">
                            {t("Demo data", "演示数据")}
                          </option>
                          <option value="live">
                            {t("Local API", "本地 API")}
                          </option>
                        </select>
                      </label>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          </div>
        </aside>
        {mobileNav && (
          <button
            className="nav-backdrop"
            aria-label={t("Close navigation", "关闭导航")}
            onClick={() => setMobileNav(false)}
          />
        )}
        <main className="console-main">
          <div className={`page ${isNew ? "creation-page" : ""}`}>
            <motion.div
              key={viewKey}
              className="view-panel"
              initial={reduced ? false : { opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: reduced ? 0 : 0.22,
                ease: [0.22, 1, 0.36, 1],
              }}
            >
            {!isShops && !operation && (
              <div className="breadcrumb">
                <a href="#/tasks" onClick={() => navigate("/tasks")}>
                  {t("Marketing work", "营销工作")}
                </a>
                <ChevronRight size={12} />
                {selectedId || isNew ? (
                  <>
                    <a href="#/tasks">{t("Tasks", "任务")}</a>
                    <ChevronRight size={12} />
                    <span>
                      {isNew
                        ? t("New", "新建")
                        : current?.product_code || selectedId}
                    </span>
                  </>
                ) : (
                  <span>
                    {isAssets
                      ? t("Product assets", "产品素材")
                      : t("Tasks", "任务")}
                  </span>
                )}
              </div>
            )}
            {error && !isShops && (
              <div className="alert" role="alert">
                <span>
                  {error} · {t("API at", "后端地址")} 127.0.0.1:8000
                </span>
                <Button
                  variant="outline"
                  onClick={() => setRevision((r) => r + 1)}
                >
                  <RefreshCw size={14} />
                  {t("Retry", "重试")}
                </Button>
              </div>
            )}
            {operation ? (
              <OperationsPage
                key={`${operation}-${demo}`}
                section={operation}
                demo={demo}
              />
            ) : isShops ? (
              <ShopsPage
                key={demo ? "demo" : "live"}
                path={path}
                demo={demo}
                notify={setToast}
              />
            ) : isNew ? (
              <CreatePage
                demo={demo}
                onCreated={(run) => {
                  setRuns((rows) => [run, ...rows]);
                  setFilter("all");
                  setQuery("");
                  setToast(
                    demo
                      ? t("Demo task created", "演示任务已创建")
                      : t("Task created", "任务已创建"),
                  );
                  go(`/tasks/${run.id}`);
                }}
                onUseTemplate={(templateId, workspace) =>
                  setStudio({ kind: "template", templateId, workspace })
                }
              />
            ) : selectedId ? (
              <>
                <div className="resource-header task-resource-header">
                  <div className="resource-name">
                    <IconButton
                      icon={ArrowLeft}
                      label={t("Back to tasks", "返回任务列表")}
                      onClick={() => go("/tasks")}
                    />
                    <p className="page-title" tabIndex={-1}>
                      {current?.name ||
                        current?.product_code ||
                        t("Task detail", "任务详情")}
                    </p>
                    {current && <Status value={current.status} />}
                  </div>
                  {current && (
                    <div className="resource-summary">
                      <span>
                        <small>{t("Current stage", "当前阶段")}</small>
                        {stageLabel(current.current_stage, locale) ||
                          t("Not started", "待执行")}
                      </span>
                      <span>
                        <small>{t("Creative mode", "创作模式")}</small>
                        {modeLabel(current.pipeline_mode, locale)}
                      </span>
                      <span>
                        <small>{t("Product code", "产品编号")}</small>
                        {current.product_code}
                      </span>
                      <span>
                        <small>{t("Updated", "更新")}</small>
                        {formatDateTime(locale, current.updated_at)}
                      </span>
                    </div>
                  )}
                  <div className="resource-actions">
                    <IconButton
                      icon={Copy}
                      label={t("Copy task ID", "复制任务编号")}
                      onClick={() =>
                        void navigator.clipboard.writeText(selectedId).then(
                          () => setToast(t("Task ID copied", "已复制任务编号")),
                          () => setToast(t("Copy failed", "复制失败")),
                        )
                      }
                    />
                    <IconButton
                      icon={RefreshCw}
                      label={t("Refresh task", "刷新任务")}
                      onClick={() =>
                        demo
                          ? setToast(t("Demo data is active", "当前为演示数据"))
                          : setRevision((r) => r + 1)
                      }
                    />
                  </div>
                </div>
                <div
                  className="resource-tabs"
                  role="group"
                  aria-label={t("Task view", "任务视图")}
                >
                  <AnimatedSelection active={detailTab}>
                    {[
                      ["overview", t("Overview", "概览")],
                      ["stages", t("Stages", "执行阶段")],
                      ["info", t("Task info", "任务信息")],
                    ].map(([id, label]) => (
                      <button
                        key={id}
                        data-selection-id={id}
                        aria-pressed={detailTab === id}
                        className={detailTab === id ? "active" : ""}
                        onClick={() => setDetailTab(id)}
                      >
                        {label}
                      </button>
                    ))}
                  </AnimatedSelection>
                </div>
                {current ? (
                  <TabPanel active={detailTab}>
                    {detailTab === "overview" ? (
                      <div className="review-workspace">
                        <TaskMediaPanel
                          key={current.id}
                          run={current}
                          demo={demo}
                          refreshKey={revision}
                          open={(run, media, trigger, options) =>
                            setPreview({ run, media, trigger, options })
                          }
                        />
                        <section
                          className="execution-section"
                          aria-label={t("Execution log", "执行记录")}
                        >
                          <div className="section-toolbar">
                            <p>{t("Stages", "执行阶段")}</p>
                            <span className="muted">
                              {t(
                                `${plan(current).length} stages`,
                                `${plan(current).length} 阶段`,
                              )}
                            </span>
                          </div>
                          <StageList run={current} demo={demo} />
                          <div className="review-action">
                            <div>
                              <Status value={current.status} />
                              <span className="muted">
                                {stageLabel(current.current_stage, locale)}
                              </span>
                            </div>
                            {current.status === "waiting_review" && !demo ? (
                              <a
                                className="button button-primary button-default"
                                href="http://127.0.0.1:8000/dashboard"
                              >
                                {t("Open full review", "打开完整审核")}
                                <ArrowUpRight size={14} />
                              </a>
                            ) : current.status === "waiting_review" ? (
                              <div className="action-buttons">
                                <Button
                                  variant="outline"
                                  disabled={busy}
                                  onClick={() => void review("reject")}
                                >
                                  <X size={15} />
                                  {t("Reject", "驳回")}
                                </Button>
                                <Button
                                  disabled={busy}
                                  onClick={() => void review("advance")}
                                >
                                  {busy ? (
                                    <LoaderCircle size={15} className="spin" />
                                  ) : (
                                    <Check size={15} />
                                  )}
                                  {t("Approve stage", "通过阶段")}
                                </Button>
                              </div>
                            ) : null}
                          </div>
                        </section>
                      </div>
                    ) : detailTab === "stages" ? (
                      <section className="surface stage-page">
                        <StageList run={current} demo={demo} />
                      </section>
                    ) : (
                      <section className="surface info-page">
                        <dl>
                          {[
                            [t("Task ID", "任务编号"), current.id],
                            [t("Product code", "产品编号"), current.product_code],
                            [t("Project", "项目"), current.project_id],
                            [
                              t("Creative mode", "创作模式"),
                              modeLabel(current.pipeline_mode, locale),
                            ],
                            [
                              t("Data source", "数据来源"),
                              demo ? t("Demo", "演示") : t("Local API", "本地 API"),
                            ],
                          ].map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>{value}</dd>
                            </div>
                          ))}
                        </dl>
                      </section>
                    )}
                  </TabPanel>
                ) : (
                  <div className="empty-state">
                    {detailLoading ? (
                      <LoaderCircle className="spin" size={24} />
                    ) : (
                      <FolderClosed size={24} />
                    )}
                    <p>
                      {detailLoading
                        ? t("Loading", "正在加载")
                        : t("Task not found", "找不到该任务")}
                    </p>
                    <Button variant="outline" onClick={() => go("/tasks")}>
                      {t("Back to list", "返回列表")}
                    </Button>
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="resource-header">
                  <p className="page-title" tabIndex={-1}>
                    {isAssets ? t("Product assets", "产品素材") : t("Tasks", "任务")}
                  </p>
                  <Button onClick={() => go("/new")}>
                    <Plus size={15} />
                    {t("New task", "新建任务")}
                  </Button>
                </div>
                <div
                  className="resource-tabs"
                  role="group"
                  aria-label={t("Task status", "任务状态")}
                >
                  <AnimatedSelection active={filter}>
                    {filters.map(([id, label]) => (
                      <button
                        key={id}
                        data-selection-id={id}
                        aria-pressed={filter === id}
                        className={filter === id ? "active" : ""}
                        onClick={() => setFilter(id)}
                      >
                        {label}
                        <span>
                          {
                            runs.filter(
                              (r) =>
                                id === "all" ||
                                r.status === id ||
                                (id === "running" && r.status === "queued"),
                            ).length
                          }
                        </span>
                      </button>
                    ))}
                  </AnimatedSelection>
                </div>
                <TabPanel active={filter}>
                  <section
                    className={isAssets ? "material-index" : "surface task-index"}
                    aria-label={
                      isAssets ? t("Product asset list", "产品素材列表") : t("Task list", "任务列表")
                    }
                    aria-busy={loading}
                  >
                  <div className="index-toolbar">
                    <label className="search">
                      <Search size={16} />
                      <input
                        id="task-search"
                        aria-label={t("Search tasks", "搜索任务")}
                        placeholder={t("Search by name or product code", "搜索名称或产品编号")}
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                      />
                      <kbd>⌘ K</kbd>
                    </label>
                    <div className="toolbar-right">
                      <label className="mode-filter">
                        <ListFilter size={14} />
                        <span className="sr-only">{t("Creative mode", "创作模式")}</span>
                        <select
                          value={modeFilter}
                          onChange={(e) => setModeFilter(e.target.value)}
                        >
                          <option value="all">{t("All modes", "全部模式")}</option>
                          {Object.entries(modes).map(([id]) => (
                            <option key={id} value={id}>
                              {modeLabel(id, locale)}
                            </option>
                          ))}
                        </select>
                      </label>
                      <IconButton
                        icon={RefreshCw}
                        label={t("Refresh list", "刷新列表")}
                        disabled={loading}
                        onClick={() =>
                          demo
                            ? setToast(t("Demo data is active", "当前为演示数据"))
                            : setRevision((r) => r + 1)
                        }
                      />
                    </div>
                  </div>
                  {isAssets ? (
                    <div className="asset-grid">
                      {visible.map((r) => (
                        <PolishedAsset href={`#/tasks/${r.id}`} key={r.id}>
                          {r.image ? (
                            <img
                              src={r.image}
                              alt={r.name || r.product_code}
                              loading="lazy"
                            />
                          ) : (
                            <div className="asset-placeholder">
                              <Image size={24} />
                            </div>
                          )}
                          <div>
                            <strong>{r.name || r.product_code}</strong>
                            <ArrowUpRight size={14} />
                          </div>
                          <small>
                            {r.product_code} · {t("Reference assets", "参考素材")}
                          </small>
                        </PolishedAsset>
                      ))}
                    </div>
                  ) : (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>{t("Task / product", "任务 / 产品")}</th>
                            <th className="task-media-column">{t("Media", "媒体")}</th>
                            <th>{t("Status", "状态")}</th>
                            <th className="mode-column">{t("Creative mode", "创作模式")}</th>
                            <th className="stage-column">{t("Current stage", "当前阶段")}</th>
                            <th className="date-column">
                              {t("Updated", "更新")} <ArrowDown size={12} />
                            </th>
                            <th>
                              <span className="sr-only">{t("Open task", "打开任务")}</span>
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {visible.map((r) => (
                            <tr key={r.id}>
                              <td>
                                <a
                                  className="task-link"
                                  href={`#/tasks/${r.id}`}
                                >
                                  <strong>
                                    {r.name ||
                                      r.product_code ||
                                      r.id.slice(0, 8)}
                                  </strong>
                                  <small>
                                    {r.product_code || r.id.slice(0, 8)}
                                  </small>
                                </a>
                              </td>
                              <td className="task-media-column">
                                <RunMediaStrip
                                  run={r}
                                  demo={demo}
                                  open={(run, media, trigger) =>
                                    setPreview({ run, media, trigger })
                                  }
                                />
                              </td>
                              <td>
                                <Status value={r.status} />
                              </td>
                              <td className="mode-column">
                                {modeLabel(r.pipeline_mode, locale) ||
                                  r.pipeline_mode}
                              </td>
                              <td className="stage-column">
                                {stageLabel(r.current_stage, locale) ||
                                  t("Not started", "待执行")}
                              </td>
                              <td className="date-column">
                                <time dateTime={r.updated_at}>
                                  {formatDateTime(locale, r.updated_at)}
                                </time>
                              </td>
                              <td>
                                <IconButton
                                  icon={ArrowRight}
                                  label={t(
                                    `View ${r.name || r.product_code}`,
                                    `查看 ${r.name || r.product_code}`,
                                  )}
                                  onClick={() => go(`/tasks/${r.id}`)}
                                />
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                  {!visible.length && (
                    <div className="empty-state">
                      {loading ? (
                        <LoaderCircle className="spin" size={24} />
                      ) : (
                        <Search size={24} />
                      )}
                      <p>
                        {loading
                          ? t("Loading", "正在加载")
                          : error
                            ? t("Connection failed", "连接失败")
                            : query || modeFilter !== "all" || filter !== "all"
                              ? t("No matching tasks", "没有匹配的任务")
                              : t("No tasks yet", "暂无任务")}
                      </p>
                      {!loading && !error && (
                        <Button
                          variant="outline"
                          onClick={() => {
                            setQuery("");
                            setModeFilter("all");
                            setFilter("all");
                          }}
                        >
                          {t("Clear filters", "清除筛选")}
                        </Button>
                      )}
                    </div>
                  )}
                  <div className="index-footer">
                    <span>
                      {t(
                        `${visible.length} tasks`,
                        `${visible.length} 个任务`,
                      )}
                    </span>
                  </div>
                  </section>
                </TabPanel>
              </>
            )}
            </motion.div>
          </div>
        </main>
        {preview && (
          <RunMediaGallery
            key={preview.run.id}
            run={preview.run}
            initial={preview.media}
            trigger={preview.trigger}
            demo={demo}
            complete={preview.options?.complete}
            onSelect={preview.options?.onSelect}
            saveTemplate={(run, media) => {
              setStudio({ kind: "asset", runId: run.id, uri: media.uri });
              setPreview(null);
            }}
            close={() => setPreview(null)}
          />
        )}
        {studio && (
          <BrandCreativeStudio
            target={studio}
            close={() => setStudio(null)}
            notify={setToast}
          />
        )}
        {toast && (
          <div className="toast" role="status">
            <Check size={15} />
            {toast}
            <IconButton
              icon={X}
              label={t("Dismiss notification", "关闭提示")}
              onClick={() => setToast("")}
            />
          </div>
        )}
      </div>
    </Tooltip.Provider>
  );
}
function StageList({ run, demo }: { run: Run; demo: boolean }) {
  const { locale, t } = useI18n();
  const stages = plan(run),
    active = stages.indexOf(run.current_stage || "");
  const items = demo
    ? stages.map((stage_name, i) => ({
        id: stage_name,
        stage_name,
        status:
          run.status === "completed" || (active >= 0 && i < active)
            ? "completed"
            : i === active
              ? run.status
              : "not_started",
        error_message: null,
      }))
    : run.stage_tasks || [];
  return (
    <ol className="stage-list">
      {items.map((stage, i) => (
        <li
          key={stage.id}
          className={stage.stage_name === run.current_stage ? "current" : ""}
        >
          <span
            className={`stage-symbol ${stage.status === "completed" ? "done" : ""}`}
          >
            {stage.status === "completed" ? (
              <Check size={13} />
            ) : (
              <span>{i + 1}</span>
            )}
          </span>
          <span className="stage-content">
            <span>{stageLabel(stage.stage_name, locale) || stage.stage_name}</span>
            {stage.error_message && (
              <small className="error-text">{stage.error_message}</small>
            )}
          </span>
          <Status value={stage.status} />
        </li>
      ))}
      {!items.length && (
        <li className="muted">{t("No execution log yet", "暂无执行记录")}</li>
      )}
    </ol>
  );
}

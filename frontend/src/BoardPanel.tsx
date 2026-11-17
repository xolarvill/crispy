/**
 * Run-detail entry point for the instruction board. The canvas itself lives in
 * the full-screen `InstructionBoard`; this panel summarises what is on the
 * board and what the agents will read.
 */
import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  Check,
  Image as ImageIcon,
  LoaderCircle,
  RefreshCw,
  SquareDashed,
  StickyNote,
} from "lucide-react";
import { Button } from "./components/ui/button";
import { useI18n } from "./i18n";
import { mediaUrl, fetchBoard, type BoardPayload } from "./boards";
import { InstructionBoard } from "./InstructionBoard";
import type { Run } from "./data";

export function BoardPanel({
  run,
  demo,
  notify,
  editVideo,
}: {
  run: Run;
  demo: boolean;
  notify?: (message: string) => void;
  editVideo?: (uri: string) => void;
}) {
  const { t } = useI18n();
  const [board, setBoard] = useState<BoardPayload | null>(null);
  const [loading, setLoading] = useState(!demo);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (demo) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError("");
    fetchBoard(run.id)
      .then((payload) => {
        if (!cancelled) setBoard(payload);
      })
      .catch((loadError: Error) => {
        if (!cancelled) setError(loadError.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [run.id, demo, tick]);

  const assetCount =
    board?.scene.items.filter((item) => item.type === "asset").length ?? 0;
  const regionCount =
    board?.scene.items.filter((item) => item.type === "region").length ?? 0;
  const noteCount =
    board?.scene.items.filter(
      (item) => item.type === "note" || item.type === "text" || item.type === "sketch",
    ).length ?? 0;
  const linkCount = board?.scene.links.length ?? 0;

  return (
    <section className="surface board-panel">
      <div className="board-panel-head">
        <div>
          <p>{t("Instruction board", "指令白板")}</p>
          <span>
            {t(
              "Mark assets, box areas and write direction the agents read literally.",
              "标注素材、框出区域、写下 agent 会照做的具体指令。",
            )}
          </span>
        </div>
        <div className="board-panel-actions">
          {board ? (
            <span className="board-panel-revision">
              {t(`Revision ${board.revision}`, `第 ${board.revision} 版`)}
            </span>
          ) : null}
          <Button disabled={demo} onClick={() => setOpen(true)}>
            <ArrowUpRight size={14} />
            {board ? t("Open board", "打开白板") : t("Create board", "新建白板")}
          </Button>
        </div>
      </div>

      {demo ? (
        <div className="board-panel-empty">
          <StickyNote size={20} />
          <p>
            {t(
              "Instruction boards need live run data.",
              "指令白板需要连接真实任务数据。",
            )}
          </p>
          <span>
            {t(
              "Switch this page to live mode (?mode=live) to annotate a run.",
              "把页面切换到实时模式（?mode=live）即可标注任务。",
            )}
          </span>
        </div>
      ) : loading ? (
        <div className="board-panel-empty">
          <LoaderCircle size={18} className="spin" />
          <p>{t("Reading the board", "正在读取白板")}</p>
        </div>
      ) : error ? (
        <div className="board-panel-empty">
          <p>{error}</p>
          <Button
            variant="outline"
            onClick={() => setTick((value) => value + 1)}
          >
            <RefreshCw size={14} />
            {t("Retry", "重试")}
          </Button>
        </div>
      ) : board ? (
        <div className="board-panel-body">
          <div className="board-panel-summary">
            <div className="board-panel-goal">
              <small>{t("Agent instruction", "给 agent 的指令")}</small>
              <p>{board.scene.goal || t("No goal written yet", "还没有写目标")}</p>
            </div>
            <dl className="board-panel-stats">
              <div>
                <dt>
                  <Check size={12} /> {t("Assets", "素材")}
                </dt>
                <dd>{assetCount}</dd>
              </div>
              <div>
                <dt>
                  <SquareDashed size={12} /> {t("Marked areas", "框选")}
                </dt>
                <dd>{regionCount}</dd>
              </div>
              <div>
                <dt>
                  <StickyNote size={12} /> {t("Notes", "笔记")}
                </dt>
                <dd>{noteCount}</dd>
              </div>
              <div>
                <dt>
                  <ArrowUpRight size={12} /> {t("Relations", "关系")}
                </dt>
                <dd>{linkCount}</dd>
              </div>
            </dl>
          </div>
          {board.snapshot_uri ? (
            <figure className="board-panel-snapshot">
              <img
                src={mediaUrl(board.snapshot_uri)}
                alt={t("Board snapshot", "白板快照")}
              />
              <figcaption>
                <ImageIcon size={12} />
                {t("Latest saved snapshot", "最近保存的快照")}
              </figcaption>
            </figure>
          ) : null}
        </div>
      ) : (
        <div className="board-panel-empty">
          <StickyNote size={22} />
          <p>{t("No instruction board yet", "还没有指令白板")}</p>
          <span>
            {t(
              "Open the board to pin run assets, circle what to keep or change, and connect references with arrows.",
              "打开白板，把任务素材钉在画布上，圈出要保留或修改的地方，用箭头连接参考素材。",
            )}
          </span>
        </div>
      )}

      {open ? (
        <InstructionBoard
          target={{ runId: run.id, title: run.name || run.product_code }}
          close={() => {
            setOpen(false);
            setTick((value) => value + 1);
          }}
          notify={notify}
          editVideo={editVideo}
        />
      ) : null}
    </section>
  );
}

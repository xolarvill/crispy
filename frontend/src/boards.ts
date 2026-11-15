/**
 * Instruction board wire types and API access.
 *
 * The scene is the server-authoritative document (see app/schemas/boards.py).
 * The canvas is only a view of it: everything the agent reads is compiled on
 * the server from these items and links.
 */
import { api, type RunMedia } from "./data";

export type BoardRole = "source" | "reference" | "style" | "output";
export type BoardLinkKind = "annotation" | "data";

type BoardNodeBase = {
  id: string;
  parent?: string | null;
  x: number;
  y: number;
};

export type BoardAssetItem = BoardNodeBase & {
  type: "asset";
  mark: number;
  assetId: string;
  uri: string;
  role: BoardRole;
  caption: string;
  w: number;
  h: number;
};

/** `x/y/w/h` are normalized to the referenced asset's own 0–1 space. */
export type BoardRegionItem = BoardNodeBase & {
  type: "region";
  assetId: string;
  w: number;
  h: number;
  note: string;
};

export type BoardNoteItem = BoardNodeBase & {
  type: "note";
  text: string;
  w: number;
  h: number;
};

export type BoardTextItem = BoardNodeBase & {
  type: "text";
  text: string;
  fontSize: number;
};

export type BoardSketchItem = BoardNodeBase & {
  type: "sketch";
  points: [number, number][];
  color: string;
  width: number;
};

export type BoardStageItem = BoardNodeBase & {
  type: "stage";
  stage: string;
  w: number;
  h: number;
  params: Record<string, unknown>;
};

export type BoardFrameItem = BoardNodeBase & {
  type: "frame";
  title: string;
  w: number;
  h: number;
};

export type BoardItem =
  | BoardAssetItem
  | BoardRegionItem
  | BoardNoteItem
  | BoardTextItem
  | BoardSketchItem
  | BoardStageItem
  | BoardFrameItem;

export type BoardLink = {
  id: string;
  kind: BoardLinkKind;
  from: string;
  to: string;
  label: string;
  port?: string;
  dataType?: string | null;
};

export type BoardScene = {
  version: 1;
  goal: string;
  items: BoardItem[];
  links: BoardLink[];
  viewport: { x: number; y: number; zoom: number };
};

export type CompiledBoard = {
  revision: number;
  digest: string;
  assets: { id: string; assetId: string; mark: number; uri: string; role: string; caption: string }[];
  regions: { id: string; assetId: string; asset_mark: number; note: string }[];
  links: { id: string; kind: string; from: string; to: string; label: string }[];
  stage_nodes: { id: string; stage: string }[];
  snapshot_uri: string;
};

export type BoardPayload = {
  id: string;
  run_id: string;
  workspace_id: string;
  title: string;
  revision: number;
  stage_scope: string | null;
  snapshot_uri: string;
  scene: BoardScene;
  compiled: CompiledBoard;
  created_at: string | null;
  updated_at: string | null;
};

export type BoardSavePayload = {
  scene: BoardScene;
  title: string;
  actor?: string;
  expected_revision: number | null;
  snapshot: string;
};

export type BoardHistoryItem = {
  revision: number;
  snapshot_uri: string;
  created_at: string | null;
};

export const MAX_ASSET_MARK = 99;

export function circledMark(mark: number) {
  if (mark >= 1 && mark <= 20) return String.fromCharCode(0x2460 + mark - 1);
  return `[${mark}]`;
}

/** Stable board node id. Assigned when a shape is created, so regions and
 * links can reference an asset before the first save. */
export function newBoardId(prefix: string) {
  const random =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID().slice(0, 8)
      : Math.random().toString(36).slice(2, 10);
  return `${prefix}_${random}`;
}

export function mediaUrl(uri: string) {
  if (/^(data:|https?:\/\/|blob:)/i.test(uri)) return uri;
  return `/api/media?path=${encodeURIComponent(uri)}`;
}

export function isVideoUri(uri: string) {
  return /\.(mp4|mov|m4v|webm)(\?|$)/i.test(uri);
}

/** The latest board, or null when this run has never had one. */
export async function fetchBoard(runId: string): Promise<BoardPayload | null> {
  try {
    return await api<BoardPayload>(`/runs/${encodeURIComponent(runId)}/board`);
  } catch (error) {
    if ((error as { status?: number }).status === 404) return null;
    throw error;
  }
}

export function fetchBoardHistory(runId: string): Promise<BoardHistoryItem[]> {
  return api<BoardHistoryItem[]>(`/runs/${encodeURIComponent(runId)}/boards`);
}

export function fetchBoardRevision(runId: string, revision: number): Promise<BoardPayload> {
  return api<BoardPayload>(
    `/runs/${encodeURIComponent(runId)}/boards/${revision}`,
  );
}

export function saveBoard(runId: string, payload: BoardSavePayload): Promise<BoardPayload> {
  return api<BoardPayload>(`/runs/${encodeURIComponent(runId)}/board`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function compileBoard(runId: string, scene: BoardScene) {
  return api<CompiledBoard>(`/runs/${encodeURIComponent(runId)}/board/compile`, {
    method: "POST",
    body: JSON.stringify({ scene }),
  });
}

export async function fetchBoardAssets(runId: string): Promise<RunMedia[]> {
  const response = await api<{ media_previews: RunMedia[] }>(
    `/runs/${encodeURIComponent(runId)}/media`,
  );
  return (response.media_previews || []).filter((item) => item.uri);
}

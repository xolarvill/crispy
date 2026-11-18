import type { ProjectJSON } from "@twick/timeline";

export type VideoSource = {
  id: string;
  name: string;
  uri: string;
  sha256: string;
  kind: "video" | "image" | "audio";
  width: number;
  height: number;
  duration_us: number;
  has_audio: boolean;
};
export type VideoClip = {
  id: string;
  kind: VideoSource["kind"] | "text";
  source_id: string | null;
  start_us: number;
  duration_us: number;
  source_in_us: number;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
  fit: "contain" | "cover";
  volume: number;
  opacity: number;
  font_size: number;
  color: string;
};
export type VideoDocument = {
  version: 1;
  width: number;
  height: number;
  fps_num: number;
  fps_den: number;
  background: string;
  tracks: { id: string; name: string; clips: VideoClip[] }[];
};
export type VideoProject = {
  id: string;
  run_id: string;
  title: string;
  revision: number;
  document: VideoDocument;
  sources: VideoSource[];
};
export type VideoJob = {
  id: string;
  revision: number;
  status: "queued" | "running" | "completed" | "cancelled" | "failed";
  progress: number;
  error: string;
  uri: string;
  artifact_id: string | null;
};
export type VideoTarget = {
  runId: string;
  uri?: string;
  projectId?: string;
  boardKey?: string;
};

export type BoardVideoLayer =
  | { id: string; kind: "image"; label: string; uri: string }
  | { id: string; kind: "text"; label: string; text: string };
export const videoMediaUrl = (uri: string) =>
  `/api/media?path=${encodeURIComponent(uri)}`;
const sdkMediaUrl = (uri: string) =>
  new URL(
    videoMediaUrl(uri),
    typeof location === "undefined" ? "http://localhost" : location.origin,
  ).href;
export const videoId = (prefix = "e") => `${prefix}-${crypto.randomUUID()}`;
export const MAX_VIDEO_TIME_US = 3_600_000_000;
export const MAX_VIDEO_TRACKS = 16;
export const MAX_VIDEO_CLIPS = 120;
export const clipEnd = (clip: VideoClip) => clip.start_us + clip.duration_us;
export const durationUs = (doc: VideoDocument) =>
  Math.max(
    0,
    ...doc.tracks.flatMap((t) =>
      t.clips.map(clipEnd),
    ),
  );
const sdkId = (id: string, prefix: string) =>
  id.startsWith(`${prefix}-`) ? id : `${prefix}-crispy-${id}`;

export const clipCount = (doc: VideoDocument) =>
  doc.tracks.reduce((count, track) => count + track.clips.length, 0);

export const findClip = (doc: VideoDocument, id: string) =>
  doc.tracks.flatMap((track) => track.clips).find((clip) => sdkId(clip.id, "e") === id);

export function frameTimeUs(doc: VideoDocument, time: number) {
  const frame = Math.round(time * doc.fps_num / doc.fps_den);
  return Math.round(frame * 1e6 * doc.fps_den / doc.fps_num);
}

/** The inspector can move within the gap around a clip without crossing its neighbours. */
export function clipTimingLimits(
  clip: VideoClip,
  previous?: VideoClip,
  next?: VideoClip,
  source?: VideoSource,
) {
  const nextStart = next?.start_us ?? MAX_VIDEO_TIME_US;
  const sourceEnd = source && source.kind !== "image" ? source.duration_us : MAX_VIDEO_TIME_US;
  return {
    minStart: previous ? clipEnd(previous) : 0,
    maxStart: nextStart - clip.duration_us,
    maxDuration: Math.min(nextStart - clip.start_us, sourceEnd - clip.source_in_us),
    maxSourceIn: sourceEnd - clip.duration_us,
  };
}

/** A frame at the playhead divides a clip into two source-contiguous ranges. */
export function cutClip(
  doc: VideoDocument,
  clip: VideoClip,
  atUs: number,
  action: "split" | "trim-start" | "trim-end",
) {
  const elapsed = atUs - clip.start_us;
  if (elapsed <= 0 || elapsed >= clip.duration_us) return doc;
  if (action === "split" && clipCount(doc) >= MAX_VIDEO_CLIPS) return doc;
  const left = { ...clip, duration_us: elapsed };
  const right = {
    ...clip,
    start_us: atUs,
    duration_us: clip.duration_us - elapsed,
    source_in_us: clip.source_in_us + (clip.kind === "video" || clip.kind === "audio" ? elapsed : 0),
  };
  const pieces = action === "split" ? [left, { ...right, id: videoId() }]
    : [action === "trim-start" ? right : left];
  return {
    ...doc,
    tracks: doc.tracks.map((track) => ({
      ...track,
      clips: track.clips.flatMap((item) => item.id === clip.id ? pieces : [item]),
    })),
  };
}

export function removeClip(doc: VideoDocument, clip: VideoClip, closeGap: boolean) {
  return {
    ...doc,
    tracks: doc.tracks.flatMap((track) => {
      if (!track.clips.some((item) => item.id === clip.id)) return [track];
      const clips = track.clips.filter((item) => item.id !== clip.id).map((item) =>
        closeGap && item.start_us >= clipEnd(clip)
          ? { ...item, start_us: item.start_us - clip.duration_us }
          : item,
      );
      return clips.length ? [{ ...track, clips }] : [];
    }),
  };
}

export function appendTrack(doc: VideoDocument, kind: VideoSource["kind"]) {
  const accepts = (clip: VideoClip) => kind === "audio"
    ? clip.kind === "audio"
    : clip.kind === "video" || clip.kind === "image";
  const prefix = kind === "audio" ? "t-audio-" : "t-picture-";
  return doc.tracks.find((track) => track.id.startsWith(prefix) && track.clips.every(accepts))
    ?? doc.tracks.find((track) => {
      if (track.id.startsWith("t-overlay-") || !track.clips.length || !track.clips.every(accepts))
        return false;
      return kind === "audio" || track.clips.some((clip) => clip.kind === "video" ||
        (clip.x === 0 && clip.y === 0 && clip.width === 1 && clip.height === 1));
    });
}

export function newClip(
  kind: VideoClip["kind"],
  source?: VideoSource,
): VideoClip {
  return {
    id: videoId(),
    kind,
    source_id: source?.id ?? null,
    start_us: 0,
    duration_us: source?.duration_us || 3_000_000,
    source_in_us: 0,
    text: kind === "text" ? "Your title" : "",
    x: 0,
    y: 0,
    width: 1,
    height: 1,
    fit: "contain",
    volume: 1,
    opacity: 1,
    font_size: 36,
    color: "#ffffff",
  };
}

/** Twick is a view. This adapter is the only place its JSON crosses our contract. */
export function toTwick(
  doc: VideoDocument,
  sources: VideoSource[],
): ProjectJSON {
  return {
    version: 1,
    tracks: doc.tracks.map((track) => ({
      id: sdkId(track.id, "t"),
      name: track.name,
      type: "element",
      props: { crispyTrackId: track.id },
      elements: track.clips.map((clip) => {
        const source = sources.find((s) => s.id === clip.source_id);
        const x = (clip.x + clip.width / 2 - 0.5) * doc.width,
          y = (clip.y + clip.height / 2 - 0.5) * doc.height;
        return {
          id: sdkId(clip.id, "e"),
          trackId: sdkId(track.id, "t"),
          type: clip.kind,
          name:
            clip.kind === "text"
              ? clip.text.slice(0, 40)
              : source?.name || clip.kind,
          s: clip.start_us / 1e6,
          e: (clip.start_us + clip.duration_us) / 1e6,
          props: {
            src: source ? sdkMediaUrl(source.uri) : undefined,
            crispySourceId: clip.source_id,
            crispyClip: clip,
            time: clip.source_in_us / 1e6,
            playbackRate: 1,
            volume: clip.volume,
            opacity: clip.opacity,
            text: clip.text,
            fontSize: clip.font_size,
            fontFamily: "Arial",
            fill: clip.color,
            textAlign: "left",
            width: clip.width * doc.width,
            height: clip.height * doc.height,
            x,
            y,
          },
          frame: {
            x,
            y,
            size: [clip.width * doc.width, clip.height * doc.height],
          },
          objectFit: clip.fit,
          mediaDuration: (source?.duration_us || 0) / 1e6,
        };
      }),
    })),
  };
}

export function fromTwick(
  project: ProjectJSON,
  previous: VideoDocument,
  sources: VideoSource[],
): VideoDocument {
  const old = new Map(
    previous.tracks.flatMap((t) =>
      t.clips.map((c) => [sdkId(c.id, "e"), c] as const),
    ),
  );
  const oldTracks = new Map(previous.tracks.map((t) => [sdkId(t.id, "t"), t]));
  return {
    ...previous,
    tracks: project.tracks.map((track) => ({
      id: oldTracks.get(track.id)?.id || track.props?.crispyTrackId || track.id,
      name: track.name,
      clips: track.elements.map((element) => {
        const source = sources.find(
          (s) =>
            s.id === element.props?.crispySourceId ||
            sdkMediaUrl(s.uri) === element.props?.src ||
            videoMediaUrl(s.uri) === element.props?.src,
        );
        const kind = element.type as VideoClip["kind"];
        const snapshot = element.props?.crispyClip as VideoClip | undefined;
        const original =
          snapshot || old.get(element.id) || newClip(kind, source);
        const id =
          snapshot && sdkId(snapshot.id, "e") === element.id
            ? snapshot.id
            : old.get(element.id)?.id || element.id;
        return {
          ...original,
          id,
          kind,
          source_id: source?.id ?? null,
          start_us: Math.max(0, Math.round(element.s * 1e6)),
          duration_us: Math.max(1, Math.round((element.e - element.s) * 1e6)),
          source_in_us: Math.max(
            0,
            Math.round(Number(element.props?.time || 0) * 1e6),
          ),
          volume: Number(element.props?.volume ?? original.volume),
          opacity: Number(element.props?.opacity ?? original.opacity),
          text: String(element.props?.text || original.text),
          font_size: Number(element.props?.fontSize || original.font_size),
          color: String(element.props?.fill || original.color),
        };
      }),
    })),
  };
}

import { useEffect, useState } from "react";
import { api } from "./api";
import { CUSTOM_AVATAR, type Look, personaFor } from "./personas";

const HAIR: Record<Look["hairStyle"], string> = {
  short: "M19.5 25 C18 11 46 11 44.5 25 C41 17 23 17 19.5 25 Z",
  side: "M19.5 27 C16 10 47 9 44.5 25 C39 15 28 19 19.5 27 Z",
  bob: "M18 38 C14 8 50 8 46 38 L42.5 38 C44 22 39 17 32 17 C25 17 20 22 21.5 38 Z",
  bun: "M19.5 25 C18 11 46 11 44.5 25 C41 17 23 17 19.5 25 Z",
  curly: "M19 27 C14 20 19 10 26 12 C29 7 37 8 39 12 C46 10 50 20 45 27 C44 20 40 17 32 17 C24 17 20 20 19 27 Z",
  cap: "M19 23 C19 9 45 9 45 23 Z",
  bald: "",
};

function Face({ look }: { look: Look }) {
  const has = (extra: Look["extras"][number]) => look.extras.includes(extra);
  return (
    <svg viewBox="0 0 64 64" role="img" aria-hidden>
      <circle cx="32" cy="32" r="32" fill={look.background} />
      <path d="M9 64 C9 49 21 45 32 45 C43 45 55 49 55 64 Z" fill={look.shirt} />
      {has("collar") && <path d="M26 45 L32 52 L38 45 L35 44 L32 47 L29 44 Z" fill="#00000022" />}
      {has("tie") && <path d="M32 47 L29.5 50 L32 62 L34.5 50 Z" fill="#c0392b" />}
      {has("lanyard") && (
        <>
          <path d="M26 45 L32 57 L38 45" fill="none" stroke="#e74c3c" strokeWidth="1.6" />
          <rect x="29" y="56" width="6" height="7" rx="1" fill="#ffffff" stroke="#00000033" />
        </>
      )}
      <rect x="28.5" y="36" width="7" height="10" rx="3" fill={look.skin} />
      <circle cx="32" cy="27" r="12.5" fill={look.skin} />
      {look.hairStyle === "bun" && <circle cx="32" cy="11" r="5" fill={look.hair} />}
      {HAIR[look.hairStyle] && <path d={HAIR[look.hairStyle]} fill={look.hair} />}
      {look.hairStyle === "cap" && (
        <path d="M17 23 L50 23 C52 23 52 26 50 26 L17 26 Z" fill={look.hair} opacity="0.85" />
      )}
      {look.hairStyle === "bald" && (
        <path d="M19.6 28 C19 22 21 19 22.5 18 M44.4 28 C45 22 43 19 41.5 18" fill="none" stroke={look.hair} strokeWidth="2.4" strokeLinecap="round" />
      )}
      <circle cx="27.5" cy="27.5" r="1.3" fill="#2a2a2a" />
      <circle cx="36.5" cy="27.5" r="1.3" fill="#2a2a2a" />
      {has("beard") && (
        <path d="M21 29 C21 42 43 42 43 29 C41 35 36 35 32 35 C28 35 23 35 21 29 Z" fill={look.hair} />
      )}
      <path d="M28 32.5 C30 34.6 34 34.6 36 32.5" fill="none" stroke={has("beard") ? "#ffffffaa" : "#2a2a2a"} strokeWidth="1.2" strokeLinecap="round" />
      {has("mustache") && <path d="M27 31 C29 29.5 31 30 32 31 C33 30 35 29.5 37 31 C35 32.4 33 32 32 31.6 C31 32 29 32.4 27 31 Z" fill={look.hair} />}
      {has("glasses") && (
        <g fill="none" stroke="#1d1d1d" strokeWidth="1.3">
          <circle cx="27.5" cy="27.5" r="3.6" />
          <circle cx="36.5" cy="27.5" r="3.6" />
          <path d="M31.1 27.5 L32.9 27.5" />
        </g>
      )}
      {has("headset") && (
        <g fill="none" stroke="#2a2a2a" strokeWidth="1.8" strokeLinecap="round">
          <path d="M19.5 27 C19.5 10 44.5 10 44.5 27" />
          <path d="M19.5 28 C19.5 35 24 36.5 28 36.5" strokeWidth="1.3" />
          <rect x="17.3" y="24.5" width="4" height="7" rx="1.8" fill="#2a2a2a" stroke="none" />
          <rect x="42.7" y="24.5" width="4" height="7" rx="1.8" fill="#2a2a2a" stroke="none" />
        </g>
      )}
    </svg>
  );
}

interface Props {
  /** Avatar key: a persona, or "custom" for an uploaded photo. */
  avatar: string;
  /** Needed to load a custom photo. */
  agentId?: number;
  size?: number;
  /** Changes when a new photo has been uploaded, to reload it. */
  version?: number;
}

export function Avatar({ avatar, agentId, size = 32, version = 0 }: Props) {
  const [photo, setPhoto] = useState<string | null>(null);

  useEffect(() => {
    if (avatar !== CUSTOM_AVATAR || agentId === undefined) {
      setPhoto(null);
      return;
    }
    let objectUrl: string | null = null;
    let cancelled = false;
    api
      .avatarBlob(agentId)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setPhoto(objectUrl);
      })
      .catch(() => setPhoto(null));
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [avatar, agentId, version]);

  return (
    <span className="avatar" style={{ width: size, height: size }}>
      {photo ? <img src={photo} alt="" /> : <Face look={personaFor(avatar).look} />}
    </span>
  );
}

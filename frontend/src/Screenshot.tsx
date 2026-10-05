import { useEffect, useState } from "react";
import { api } from "./api";

export function Screenshot({ agentId, filename }: { agentId: number; filename: string }) {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    api
      .screenshotBlob(agentId, filename)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [agentId, filename]);

  if (!url) return null;
  return (
    <div className="screenshot">
      <img src={url} alt="Browser screenshot taken by the agent" />
    </div>
  );
}

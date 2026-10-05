import RFB from "@novnc/novnc";
import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Agent } from "./types";

type Connection = "connecting" | "connected" | "disconnected";

/** The agent's VM desktop over VNC, view-only until the user takes control. */
export function Computer({ agent }: { agent: Agent }) {
  const screen = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [manual, setManual] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!screen.current) return;
    setConnection("connecting");
    const client = new RFB(screen.current, api.vncUrl(agent.id), {
      wsProtocols: api.vncProtocols(),
    });
    client.viewOnly = true;
    client.scaleViewport = true;
    client.background = "#000";
    client.addEventListener("connect", () => setConnection("connected"));
    client.addEventListener("disconnect", () => setConnection("disconnected"));
    rfb.current = client;
    return () => {
      rfb.current = null;
      client.disconnect();
    };
  }, [agent.id, attempt]);

  // Hand control back to the agent when the panel closes.
  useEffect(() => {
    return () => {
      void api.setManualControl(agent.id, false).catch(() => undefined);
    };
  }, [agent.id]);

  async function toggleControl() {
    const next = !manual;
    await api.setManualControl(agent.id, next);
    setManual(next);
    if (rfb.current) {
      rfb.current.viewOnly = !next;
      if (next) rfb.current.focus();
    }
  }

  return (
    <section className="computer">
      <div className="computer-bar">
        <span className="computer-title">
          {agent.name}'s computer
          <span className="muted">
            {connection === "connected"
              ? manual
                ? " · you are in control, the agent is paused"
                : " · watching"
              : ` · ${connection}`}
          </span>
        </span>
        {connection === "disconnected" ? (
          <button onClick={() => setAttempt((n) => n + 1)}>Reconnect</button>
        ) : (
          <button
            className={manual ? "primary" : ""}
            disabled={connection !== "connected"}
            onClick={() => void toggleControl()}
          >
            {manual ? "Return control to agent" : "Take control"}
          </button>
        )}
      </div>
      <div className={manual ? "screen manual" : "screen"} ref={screen} />
    </section>
  );
}

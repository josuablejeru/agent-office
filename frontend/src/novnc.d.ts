// Minimal typing for the parts of noVNC this app uses.
declare module "@novnc/novnc" {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { wsProtocols?: string[] });
    viewOnly: boolean;
    scaleViewport: boolean;
    background: string;
    disconnect(): void;
    focus(): void;
  }
}

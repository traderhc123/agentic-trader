// The saved settings as the agent reports them (GET /api/v2/settings): read
// when a settings page opens, and again after every save, so a page always
// shows what was saved, never what it sent.
import { useCallback, useRef, useState } from "react";
import type { SettingsView } from "../lib/settingsApi";
import { API, getJson } from "./lib/http";

export interface SettingsFeed {
  /** the last good read; null before the first */
  view: SettingsView | null;
  /** why the last read failed ("" when it did not) */
  error: string;
  /** a write's outcome is not known (see http.onUnknownWrite): `view` may
   *  not be what is saved, until a read succeeds */
  unsure: boolean;
  loading: boolean;
  /** read again; resolves to the new view (null when the read failed) */
  reload: () => Promise<SettingsView | null>;
  markUnsure: () => void;
}

export function useSettings(): SettingsFeed {
  const [view, setView] = useState<SettingsView | null>(null);
  const [error, setError] = useState("");
  const [unsure, setUnsure] = useState(false);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  const reload = useCallback(async () => {
    const n = ++seq.current;
    setLoading(true);
    const r = await getJson<SettingsView>(API.settings);
    if (n !== seq.current) return null; // a newer read is on its way
    setLoading(false);
    if (!r.ok || r.body.ok !== true) {
      setError(r.ok ? "The agent answered in a form this page can't read. Try again." : r.error);
      return null;
    }
    setError("");
    setUnsure(false);
    setView(r.body);
    return r.body;
  }, []);
  const markUnsure = useCallback(() => setUnsure(true), []);
  return { view, error, unsure, loading, reload, markUnsure };
}

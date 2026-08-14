import { useEffect, useState, useCallback } from "react";
import { toast } from "sonner";
import { Plus, Trash2, Loader2, ListChecks } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import api, { formatApiError } from "@/lib/api";

function ProviderBadge({ provider }) {
  if (provider === "outlook_graph") {
    return (
      <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded text-xs font-medium bg-sky-500/10 text-sky-400 border border-sky-500/20">
        Outlook · Graph
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
      Gmail · IMAP
    </span>
  );
}

export default function AssignmentsTab({ onChanged }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [email, setEmail] = useState("");
  const [provider, setProvider] = useState("outlook_graph");
  const [label, setLabel] = useState("");
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("/assignments");
      setRows(data);
    } catch (err) {
      toast.error(formatApiError(err.response?.data?.detail));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const add = async (e) => {
    e.preventDefault();
    setAdding(true);
    try {
      await api.post("/assignments", { email, provider, label });
      toast.success("Assignment added");
      setEmail("");
      setLabel("");
      await load();
      onChanged?.();
    } catch (err) {
      toast.error(formatApiError(err.response?.data?.detail));
    } finally {
      setAdding(false);
    }
  };

  const changeProvider = async (id, newProvider) => {
    try {
      await api.patch(`/assignments/${id}`, { provider: newProvider });
      await load();
      onChanged?.();
    } catch (err) {
      toast.error(formatApiError(err.response?.data?.detail));
    }
  };

  const remove = async (id) => {
    try {
      await api.delete(`/assignments/${id}`);
      toast.success("Assignment removed");
      await load();
      onChanged?.();
    } catch (err) {
      toast.error(formatApiError(err.response?.data?.detail));
    }
  };

  return (
    <div className="fade-up space-y-6">
      <div>
        <h1 className="font-display text-3xl sm:text-4xl font-extrabold tracking-tight text-white">Email Assignments</h1>
        <p className="text-sm text-slate-400 mt-2 max-w-xl leading-relaxed">
          Map each Netflix recipient address to its provider. Outlook addresses fetch via Microsoft Graph; Gmail
          addresses use the existing catch-all IMAP path.
        </p>
      </div>

      <form onSubmit={add} className="h24-card p-5 flex flex-col md:flex-row gap-3 md:items-end">
        <div className="flex-1 space-y-1.5">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Netflix email</label>
          <Input
            data-testid="assignment-email-input"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="user@outlook.com"
            className="bg-black/40 border-white/10 text-slate-100"
          />
        </div>
        <div className="w-full md:w-44 space-y-1.5">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Provider</label>
          <Select value={provider} onValueChange={setProvider}>
            <SelectTrigger data-testid="assignment-provider-select" className="bg-black/40 border-white/10 text-slate-100">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="outlook_graph">Outlook (Graph)</SelectItem>
              <SelectItem value="gmail_imap">Gmail (IMAP)</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="w-full md:w-40 space-y-1.5">
          <label className="text-xs text-slate-500 uppercase tracking-wide">Label (opt)</label>
          <Input
            data-testid="assignment-label-input"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Family plan"
            className="bg-black/40 border-white/10 text-slate-100"
          />
        </div>
        <Button
          type="submit"
          data-testid="add-assignment-button"
          disabled={adding}
          className="bg-[#E50914] hover:bg-[#c40810] text-white font-semibold h-10"
        >
          {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4 mr-1" />}
          Add
        </Button>
      </form>

      <div data-testid="email-assignments-table" className="h24-card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16 text-slate-500">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : rows.length === 0 ? (
          <div className="p-12 text-center">
            <ListChecks className="h-10 w-10 text-slate-600 mx-auto mb-3" />
            <p className="text-slate-400">No assignments yet. Add one above.</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500 text-xs uppercase tracking-wide border-b border-white/10">
                <th className="px-5 py-3 font-medium">Email</th>
                <th className="px-5 py-3 font-medium">Provider</th>
                <th className="px-5 py-3 font-medium">Status</th>
                <th className="px-5 py-3 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="px-5 py-3.5 text-slate-200">
                    <div className="font-medium">{r.email_norm}</div>
                    {r.label && <div className="text-xs text-slate-500">{r.label}</div>}
                  </td>
                  <td className="px-5 py-3.5">
                    <Select value={r.provider} onValueChange={(v) => changeProvider(r.id, v)}>
                      <SelectTrigger className="h-8 w-40 bg-black/30 border-white/10 text-xs text-slate-200">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="outlook_graph">Outlook (Graph)</SelectItem>
                        <SelectItem value="gmail_imap">Gmail (IMAP)</SelectItem>
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="px-5 py-3.5">
                    {r.provider === "outlook_graph" ? (
                      r.mailbox_status === "connected" ? (
                        <span className="text-emerald-400 text-xs font-medium">● Connected</span>
                      ) : r.mailbox_status === "needs_reconnect" ? (
                        <span className="text-rose-400 text-xs font-medium">● Needs reconnect</span>
                      ) : (
                        <span className="text-slate-500 text-xs">○ Not connected</span>
                      )
                    ) : (
                      <ProviderBadge provider={r.provider} />
                    )}
                  </td>
                  <td className="px-5 py-3.5 text-right">
                    <Button
                      data-testid={`delete-assignment-${r.email_norm}`}
                      onClick={() => remove(r.id)}
                      variant="ghost"
                      size="sm"
                      className="text-slate-500 hover:text-rose-400 hover:bg-rose-500/10"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

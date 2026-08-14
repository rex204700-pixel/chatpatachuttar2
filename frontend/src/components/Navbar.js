import { Play, LogOut, Inbox, ListChecks, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/context/AuthContext";

const TABS = [
  { key: "mailboxes", label: "Mailboxes", icon: Inbox, testid: "nav-mailboxes-tab" },
  { key: "assignments", label: "Assignments", icon: ListChecks, testid: "nav-assignments-tab" },
  { key: "search", label: "Code Search", icon: Search, testid: "nav-code-search-tab" },
];

export default function Navbar({ active, onChange, msConfigured }) {
  const { user, logout } = useAuth();

  return (
    <header
      data-testid="nav-header"
      className="sticky top-0 z-40 glass border-b border-white/10 h-16 flex items-center"
    >
      <div className="max-w-7xl mx-auto w-full px-4 sm:px-6 lg:px-8 flex items-center justify-between">
        <div className="flex items-center gap-8">
          <div className="flex items-center gap-2.5">
            <div className="h-8 w-8 rounded-lg bg-[#E50914] flex items-center justify-center">
              <Play className="h-4 w-4 text-white fill-white" />
            </div>
            <span className="font-display font-extrabold text-white tracking-tight hidden sm:block">
              himawari<span className="text-[#E50914]">24</span>
            </span>
          </div>
          <nav className="flex items-center gap-1">
            {TABS.map((t) => {
              const Icon = t.icon;
              const isActive = active === t.key;
              return (
                <button
                  key={t.key}
                  data-testid={t.testid}
                  onClick={() => onChange(t.key)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                    isActive
                      ? "bg-white/10 text-white"
                      : "text-slate-400 hover:text-slate-200 hover:bg-white/5"
                  }`}
                >
                  <Icon className="h-4 w-4" />
                  <span className="hidden sm:inline">{t.label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          <span
            className={`hidden md:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${
              msConfigured
                ? "bg-sky-500/10 text-sky-400 border-sky-500/25"
                : "bg-amber-500/10 text-amber-400 border-amber-500/25"
            }`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${msConfigured ? "bg-sky-400" : "bg-amber-400"}`} />
            {msConfigured ? "Microsoft OAuth ready" : "Microsoft not configured"}
          </span>
          <span className="text-xs text-slate-500 hidden lg:block">{user?.email}</span>
          <Button
            data-testid="admin-logout-button"
            onClick={logout}
            variant="ghost"
            size="sm"
            className="text-slate-400 hover:text-white hover:bg-white/5"
          >
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </header>
  );
}

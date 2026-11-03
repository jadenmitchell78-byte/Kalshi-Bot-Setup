'use client';
import React, { useState, useEffect, useRef } from 'react';
import Hls from 'hls.js';

// ============================================================================
// 1. Hybrid Media Player (<iframe> + HLS.js Hard Reset & Zero Collision)
// ============================================================================
export interface StreamFeed {
  label: string;
  type?: 'iframe' | 'hls';
  url: string;
}

export interface StreamEvent {
  id: string;
  shortName: string;
  description: string;
  state?: string;
  clock?: string;
  away?: { name: string; abbr: string; score: string; logo: string };
  home?: { name: string; abbr: string; score: string; logo: string };
  feeds: StreamFeed[];
}

export function LiveWireSlotPlayer({
  slotId,
  activeEvent,
  feedIndex = 0,
  isAudioActive = false,
  onSelectFeed,
  onOpenCatalog,
  onClearSlot,
}: {
  slotId: number;
  activeEvent: StreamEvent | null;
  feedIndex?: number;
  isAudioActive?: boolean;
  onSelectFeed?: (slotId: number, feedIdx: number) => void;
  onOpenCatalog?: (slotId: number) => void;
  onClearSlot?: (slotId: number) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [, setStatus] = useState<string>('READY');

  const feeds = activeEvent?.feeds || [];
  const currentFeed = feeds[feedIndex] || feeds[0] || null;
  const isM3U8 = Boolean(
    currentFeed?.url &&
      (/\.m3u8(\?|$)/i.test(currentFeed.url) || currentFeed.url.toLowerCase().includes('.m3u8') || currentFeed.type === 'hls')
  );

  useEffect(() => {
    // 1. Hard reset: always destroy any existing HLS instance on feed switch
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }

    if (!currentFeed || !isM3U8 || !videoRef.current) return;

    const video = videoRef.current;
    setStatus('BUFFERING');

    if (Hls.isSupported()) {
      const hls = new Hls({
        enableWorker: true,
        lowLatencyMode: true,
        backBufferLength: 30,
        manifestLoadingTimeOut: 6000, // Hard 6s timeout so it never hangs
      });
      hlsRef.current = hls;
      hls.loadSource(currentFeed.url);
      hls.attachMedia(video);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        setStatus('LIVE');
        video.muted = !isAudioActive;
        video.play().catch(() => {});
      });

      hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          setStatus('ERROR');
          hls.destroy();
        }
      });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = currentFeed.url;
      video.muted = !isAudioActive;
      video.addEventListener('loadedmetadata', () => {
        setStatus('LIVE');
        video.play().catch(() => {});
      });
    }

    return () => {
      if (hlsRef.current) {
        hlsRef.current.destroy();
        hlsRef.current = null;
      }
      if (video) {
        video.removeAttribute('src');
        video.load();
      }
    };
  }, [currentFeed?.url, isM3U8, slotId]);

  // Sync mute state without reloading the canvas
  useEffect(() => {
    if (videoRef.current) {
      videoRef.current.muted = !isAudioActive;
    }
  }, [isAudioActive]);

  // Empty Slot State -> Opens Clean Event Catalog Drawer
  if (!activeEvent || !currentFeed) {
    return (
      <div className="relative w-full aspect-video bg-[#070b14] border border-cyan-500/30 rounded-2xl flex flex-col items-center justify-center p-4 shadow-xl">
        <div className="w-12 h-12 rounded-full bg-cyan-500/10 border border-cyan-500/40 flex items-center justify-center text-xl mb-3 shadow-[0_0_16px_rgba(6,182,212,0.35)]">
          📺
        </div>
        <p className="text-xs text-slate-400 uppercase tracking-widest mb-1 font-bold">Slot {slotId + 1} Available</p>
        <p className="text-[11px] text-slate-500 mb-3 text-center max-w-[260px]">
          Mount live games with team logos, scores, and direct server feeds
        </p>
        <button
          onClick={() => onOpenCatalog && onOpenCatalog(slotId)}
          className="px-4 py-2 rounded-xl bg-cyan-500/20 border border-cyan-400 text-cyan-300 text-xs font-bold hover:bg-cyan-500/30 shadow-[0_0_14px_rgba(6,182,212,0.3)] transition"
        >
          + Browse Event Catalog (Slot {slotId + 1})
        </button>
      </div>
    );
  }

  return (
    <div className="relative w-full bg-[#05080f] border border-cyan-500/40 rounded-2xl overflow-hidden shadow-2xl">
      {/* Top Tactical Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-[#0b1220] border-b border-slate-800">
        <div className="flex items-center gap-2 min-w-0">
          <span
            className={`px-2 py-0.5 text-[10px] font-extrabold rounded whitespace-nowrap ${
              isAudioActive
                ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-400/50 shadow-[0_0_8px_rgba(6,182,212,0.4)]'
                : 'bg-slate-800 text-slate-400'
            }`}
          >
            {isAudioActive ? '🔊 AUDIO ACTIVE' : '🔇 MUTED'}
          </span>
          <span className="text-xs font-bold text-white truncate max-w-[180px]">
            {activeEvent.shortName}
          </span>
        </div>

        <div className="flex items-center gap-1.5 flex-shrink-0">
          <button
            onClick={() => onOpenCatalog && onOpenCatalog(slotId)}
            className="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-[11px] text-cyan-300 font-bold transition"
            title="Browse events for this slot"
          >
            📋 Catalog
          </button>
          <span className="px-2 py-1 rounded-lg bg-emerald-500/20 text-emerald-300 text-[11px] font-semibold">
            Feed {feedIndex + 1}/{feeds.length || 1}
          </span>
          {onClearSlot && (
            <button
              onClick={() => onClearSlot(slotId)}
              className="px-2 py-1 rounded-lg bg-red-500/20 hover:bg-red-500/30 text-[11px] text-red-400 font-bold transition"
              title="Close stream"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      {/* Main 16:9 Media Stage — Fixed Aspect Ratio prevents 1px collapsed horizontal line */}
      <div className="relative w-full aspect-video bg-black flex items-center justify-center overflow-hidden">
        {isM3U8 ? (
          <video
            key={`hls-${slotId}-${currentFeed.url}`}
            ref={videoRef}
            playsInline
            autoPlay
            muted={!isAudioActive}
            controls
            className="w-full h-full object-contain bg-black border-0"
          />
        ) : (
          <iframe
            key={`iframe-${slotId}-${currentFeed.url}`}
            src={currentFeed.url}
            title={`${activeEvent.shortName} - Slot ${slotId + 1}`}
            className="w-full h-full border-0 bg-black"
            allow="accelerometer *; autoplay *; clipboard-write *; encrypted-media *; gyroscope *; picture-in-picture *; web-share *; fullscreen *"
            allowFullScreen
            referrerPolicy="no-referrer"
          />
        )}
      </div>

      {/* Bottom Direct Feed Switcher Bar */}
      <div className="flex items-center gap-2 px-3 py-2.5 bg-[#090e1a] overflow-x-auto no-scrollbar border-t border-slate-800/80">
        <span className="text-[10px] text-slate-400 font-bold uppercase tracking-wider flex-shrink-0">Feeds:</span>
        {feeds.map((f, idx) => (
          <button
            key={f.label || idx}
            onClick={() => onSelectFeed && onSelectFeed(slotId, idx)}
            className={`px-3 py-1.5 rounded-full text-xs font-bold whitespace-nowrap transition ${
              idx === feedIndex
                ? 'bg-cyan-500 text-slate-950 shadow-[0_0_12px_rgba(6,182,212,0.6)]'
                : 'bg-slate-800/90 text-slate-300 hover:bg-slate-700'
            }`}
          >
            {f.label || `ADMIN #${idx + 1} HD`}
          </button>
        ))}

        {currentFeed.url && (
          <a
            href={currentFeed.url}
            target="_blank"
            rel="noopener noreferrer"
            className="ml-auto px-3 py-1.5 rounded-full text-xs font-semibold bg-slate-800/70 border border-cyan-500/40 text-cyan-300 hover:bg-cyan-500/20 whitespace-nowrap transition"
          >
            ↗ Popout (Unblock)
          </a>
        )}
      </div>
    </div>
  );
}

// Backward compatible export for single player views
export function StreamPlayer({ streamUrl }: { streamUrl: string }) {
  const dummyEvent: StreamEvent = {
    id: 'single',
    shortName: 'Live Wire',
    description: 'Direct Broadcast Feed',
    feeds: [{ label: 'DIRECT', url: streamUrl }],
  };
  return <LiveWireSlotPlayer slotId={0} activeEvent={dummyEvent} isAudioActive={true} />;
}

// ============================================================================
// 2. Event Catalog Modal (ESPN Scores, Official Team Logos & Direct Feeds)
// ============================================================================
const SPORT_ENDPOINTS: Record<string, string> = {
  NBA: 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard',
  NFL: 'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard',
  SOCCER: 'https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard',
};

export function LiveWireCatalogModal({
  targetSlot = 0,
  onSelectEvent,
  onClose,
  streamSourcesMap = {},
}: {
  targetSlot?: number;
  onSelectEvent: (slotId: number, event: StreamEvent) => void;
  onClose: () => void;
  streamSourcesMap?: Record<string, StreamFeed[]>;
}) {
  const [sport, setSport] = useState<string>('NBA');
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let active = true;
    setLoading(true);

    fetch(SPORT_ENDPOINTS[sport] || SPORT_ENDPOINTS.NBA)
      .then((r) => r.json())
      .then((data) => {
        if (!active) return;
        const parsed: StreamEvent[] = (data.events || []).map((ev: any) => {
          const comp = ev.competitions?.[0] || {};
          const away = comp.competitors?.find((c: any) => c.homeAway === 'away') || {};
          const home = comp.competitors?.find((c: any) => c.homeAway === 'home') || {};
          const awayAbbr = away.team?.abbreviation || 'AWAY';
          const homeAbbr = home.team?.abbreviation || 'HOME';

          return {
            id: ev.id,
            shortName: `${away.team?.shortDisplayName || awayAbbr} vs ${home.team?.shortDisplayName || homeAbbr}`,
            description: `${comp.venue?.fullName || 'Live Arena'} • ${ev.status?.type?.detail || 'Scheduled'}`,
            state: ev.status?.type?.state || 'pre',
            clock: ev.status?.type?.shortDetail || 'Pregame',
            away: {
              name: away.team?.displayName || awayAbbr,
              abbr: awayAbbr,
              score: away.score || '0',
              logo: away.team?.logo || `https://a.espncdn.com/i/teamlogos/nba/500/${awayAbbr.toLowerCase()}.png`,
            },
            home: {
              name: home.team?.displayName || homeAbbr,
              abbr: homeAbbr,
              score: home.score || '0',
              logo: home.team?.logo || `https://a.espncdn.com/i/teamlogos/nba/500/${homeAbbr.toLowerCase()}.png`,
            },
            feeds: streamSourcesMap[ev.id] || [
              { label: 'ADMIN #1 HD', type: 'iframe', url: `https://embedsports.top/embed/admin/${ev.id}/1` },
              { label: 'ADMIN #2 HD', type: 'iframe', url: `https://embedsports.top/embed/admin/${ev.id}/2` },
              { label: 'ADMIN #3 HD', type: 'iframe', url: `https://embedsports.top/embed/admin/${ev.id}/3` },
              { label: 'THE TV APP', type: 'iframe', url: `https://thetvapp.to/${sport.toLowerCase()}` },
            ],
          };
        });
        setEvents(parsed);
        setLoading(false);
      })
      .catch(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [sport, streamSourcesMap]);

  return (
    <div className="fixed inset-0 z-50 bg-black/85 backdrop-blur-md flex flex-col p-4 md:p-6 overflow-y-auto">
      <div className="flex items-center justify-between pb-3 border-b border-slate-800 max-w-4xl w-full mx-auto">
        <div>
          <h2 className="text-base md:text-lg font-bold text-white flex items-center gap-2">
            <span>⚡ Live Wire Event Catalog</span>
          </h2>
          <p className="text-xs text-cyan-400">Select an event to mount directly into Slot {targetSlot + 1}</p>
        </div>
        <button
          onClick={onClose}
          className="px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-bold transition"
        >
          ✕ Close
        </button>
      </div>

      {/* Sport Filter Tabs */}
      <div className="flex gap-2 my-4 max-w-4xl w-full mx-auto">
        {Object.keys(SPORT_ENDPOINTS).map((s) => (
          <button
            key={s}
            onClick={() => setSport(s)}
            className={`px-4 py-1.5 rounded-xl text-xs font-bold transition ${
              sport === s
                ? 'bg-cyan-500 text-slate-950 shadow-[0_0_12px_rgba(6,182,212,0.5)]'
                : 'bg-slate-900 text-slate-400 border border-slate-800 hover:bg-slate-800'
            }`}
          >
            {s}
          </button>
        ))}
      </div>

      {/* Clean Logo & Description Event Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3 max-w-4xl w-full mx-auto">
        {loading ? (
          <div className="col-span-full text-center py-12 text-xs text-slate-400">
            <div className="w-8 h-8 border-2 border-cyan-400 border-t-transparent rounded-full animate-spin mx-auto mb-2" />
            Loading live {sport} catalog...
          </div>
        ) : events.length === 0 ? (
          <div className="col-span-full text-center py-12 text-xs text-slate-400">
            No live or upcoming {sport} games currently scheduled.
          </div>
        ) : (
          events.map((ev) => (
            <div
              key={ev.id}
              onClick={() => {
                onSelectEvent(targetSlot, ev);
                onClose();
              }}
              className="p-3.5 rounded-2xl bg-[#0b1220] border border-slate-800 hover:border-cyan-400/60 cursor-pointer transition flex flex-col gap-2 shadow-lg hover:shadow-[0_4px_20px_rgba(6,182,212,0.15)] group"
            >
              <div className="flex items-center justify-between">
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                    ev.state === 'in'
                      ? 'bg-red-500/20 text-red-400 border border-red-500/40 animate-pulse'
                      : 'bg-slate-800 text-slate-400'
                  }`}
                >
                  {ev.state === 'in' ? `🔴 LIVE • ${ev.clock}` : ev.clock}
                </span>
                <span className="text-[11px] text-cyan-400 font-semibold group-hover:translate-x-1 transition-transform">
                  Load in Slot {targetSlot + 1} →
                </span>
              </div>

              <div className="flex items-center justify-between my-1">
                <div className="flex items-center gap-2.5 min-w-0">
                  {ev.away?.logo && (
                    <img src={ev.away.logo} alt={ev.away.abbr} className="w-8 h-8 object-contain flex-shrink-0" />
                  )}
                  <div className="min-w-0">
                    <div className="text-sm font-bold text-white truncate">{ev.away?.name}</div>
                    <div className="text-[10px] text-slate-400">Away</div>
                  </div>
                </div>
                <div className="text-base md:text-lg font-extrabold text-white px-2 whitespace-nowrap">
                  {ev.away?.score} <span className="text-slate-600 text-xs mx-1">vs</span> {ev.home?.score}
                </div>
                <div className="flex items-center gap-2.5 text-right min-w-0">
                  <div className="min-w-0">
                    <div className="text-sm font-bold text-white truncate">{ev.home?.name}</div>
                    <div className="text-[10px] text-slate-400">Home</div>
                  </div>
                  {ev.home?.logo && (
                    <img src={ev.home.logo} alt={ev.home.abbr} className="w-8 h-8 object-contain flex-shrink-0" />
                  )}
                </div>
              </div>

              <p className="text-[11px] text-slate-400 truncate border-t border-slate-800/80 pt-1.5">
                🏟 {ev.description}
              </p>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ============================================================================
// 3. Multi-View Orchestrator with Sweat Cards
// ============================================================================
export default function WatchLiveWire({
  streamUrl: propStreamUrl,
  activeLegs = [],
}: {
  streamUrl?: string;
  activeLegs?: any[];
}) {
  const [layout, setLayout] = useState<number>(1); // 1, 2, or 4
  const [catalogOpen, setCatalogOpen] = useState<boolean>(false);
  const [targetSlot, setTargetSlot] = useState<number>(0);
  const [audioFocus, setAudioFocus] = useState<number>(0);

  const [slotEvents, setSlotEvents] = useState<(StreamEvent | null)[]>([
    null, null, null, null,
  ]);
  const [slotFeedIndices, setSlotFeedIndices] = useState<number[]>([0, 0, 0, 0]);

  // Initial stream population
  useEffect(() => {
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      const queryStream = params.get('streamUrl') || params.get('stream');
      const sport = (params.get('sport') || 'NBA').toUpperCase();
      const initialUrl = queryStream || propStreamUrl || `https://thetvapp.to/${sport.toLowerCase()}`;

      setSlotEvents((prev) => {
        const next = [...prev];
        if (!next[0]) {
          next[0] = {
            id: 'init-0',
            shortName: `${sport} Live Wire`,
            description: 'Live Broadcast Edge',
            feeds: [
              { label: 'DIRECT HD', type: 'iframe', url: initialUrl },
              { label: 'ADMIN #1', type: 'iframe', url: 'https://embedsports.top/' },
              { label: 'ADMIN #2', type: 'iframe', url: 'https://v2.sportsurge.net/' },
            ],
          };
        }
        return next;
      });
    }
  }, [propStreamUrl]);

  const handleSelectFeed = (slotId: number, feedIdx: number) => {
    setSlotFeedIndices((prev) => {
      const next = [...prev];
      next[slotId] = feedIdx;
      return next;
    });
  };

  const handleOpenCatalog = (slotId: number) => {
    setTargetSlot(slotId);
    setCatalogOpen(true);
  };

  const handleSelectEvent = (slotId: number, event: StreamEvent) => {
    setSlotEvents((prev) => {
      const next = [...prev];
      next[slotId] = event;
      return next;
    });
    setSlotFeedIndices((prev) => {
      const next = [...prev];
      next[slotId] = 0;
      return next;
    });
    setAudioFocus(slotId);
  };

  const handleClearSlot = (slotId: number) => {
    setSlotEvents((prev) => {
      const next = [...prev];
      next[slotId] = null;
      return next;
    });
  };

  return (
    <div className="w-full min-h-[100dvh] bg-[#06080e] text-white flex flex-col font-sans select-none">
      {/* Top Navbar */}
      <header className="sticky top-0 z-30 flex items-center justify-between px-4 py-3 bg-[#0b101b]/95 backdrop-blur border-b border-white/10">
        <div className="flex items-center gap-3">
          <div className="text-sm font-extrabold tracking-wide flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
            <span className="bg-gradient-to-r from-cyan-400 to-sky-200 bg-clip-text text-transparent">
              LIVE WIRE MULTI-VIEW
            </span>
          </div>
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 font-bold">
            HYBRID V2
          </span>
        </div>

        {/* Layout & Audio Controls */}
        <div className="flex items-center gap-2">
          <div className="flex bg-[#101726] rounded-xl p-0.5 border border-white/10">
            {[1, 2, 4].map((n) => (
              <button
                key={n}
                onClick={() => setLayout(n)}
                className={`px-2.5 py-1 rounded-lg text-xs font-bold transition ${
                  layout === n ? 'bg-cyan-500 text-slate-950 shadow-[0_0_10px_rgba(6,182,212,0.5)]' : 'text-slate-400 hover:text-white'
                }`}
              >
                {n === 1 ? 'Solo' : n === 2 ? 'Dual' : 'Quad'}
              </button>
            ))}
          </div>
          <button
            onClick={() => handleOpenCatalog(targetSlot)}
            className="px-3 py-1.5 rounded-xl bg-cyan-500/20 border border-cyan-400 text-cyan-300 text-xs font-bold hover:bg-cyan-500/30 transition flex items-center gap-1.5"
          >
            <span>📋</span> Catalog
          </button>
        </div>
      </header>

      {/* Media Player Grid */}
      <main className="p-3 md:p-4 flex-1 space-y-4">
        <div
          className={`grid gap-3 ${
            layout === 1 ? 'grid-cols-1' : layout === 2 ? 'grid-cols-1 md:grid-cols-2' : 'grid-cols-1 md:grid-cols-2'
          }`}
        >
          {Array.from({ length: layout }).map((_, slotIdx) => (
            <div key={slotIdx} onClick={() => setAudioFocus(slotIdx)}>
              <LiveWireSlotPlayer
                slotId={slotIdx}
                activeEvent={slotEvents[slotIdx]}
                feedIndex={slotFeedIndices[slotIdx]}
                isAudioActive={audioFocus === slotIdx}
                onSelectFeed={handleSelectFeed}
                onOpenCatalog={handleOpenCatalog}
                onClearSlot={handleClearSlot}
              />
            </div>
          ))}
        </div>

        {/* Live Sweat Parlay Cards */}
        <div className="pt-2 border-t border-slate-800/80">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-bold uppercase tracking-wider text-slate-400">
              Active Auto-Parlay & ATD Sweat
            </span>
            <span className="text-xs font-mono text-emerald-400">⚡ Real-Time Edge Active</span>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {(activeLegs.length
              ? activeLegs
              : [
                  { title: '3-Team Parlay', sub: 'Pistons vs 76ers', odds: '+450', payout: '+$27.50', pct: 78 },
                  { title: 'Live Prop Sniper', sub: 'C. Cunningham O 22.5', odds: '+145', payout: '+$11.55', pct: 85 },
                  { title: 'NFL ATD Radar', sub: 'Bears @ Seahawks', odds: '+210', payout: '+$18.00', pct: 60 },
                  { title: 'RedZone Surge', sub: '4th & Goal Drive', odds: '+320', payout: '+$32.00', pct: 90 },
                ]
            ).map((card, i) => (
              <div key={i} className="bg-[#0b101b] border border-slate-800 rounded-xl p-3 shadow-lg">
                <div className="flex justify-between items-start mb-1">
                  <span className="text-xs font-bold text-white truncate mr-1">{card.title}</span>
                  <span className="text-xs font-extrabold text-emerald-400 whitespace-nowrap">{card.payout}</span>
                </div>
                <p className="text-[11px] text-slate-400 mb-2 truncate">{card.sub}</p>
                <div className="flex justify-between items-center mb-1.5">
                  <span className="text-[10px] font-mono text-slate-300">ODDS: {card.odds}</span>
                  <span className="bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 text-[9px] font-extrabold px-1.5 py-0.5 rounded">
                    WINNING
                  </span>
                </div>
                <div className="w-full h-1.5 bg-slate-800 rounded-full overflow-hidden">
                  <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${card.pct}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </main>

      {/* Event Catalog Modal Drawer */}
      {catalogOpen && (
        <LiveWireCatalogModal
          targetSlot={targetSlot}
          onSelectEvent={handleSelectEvent}
          onClose={() => setCatalogOpen(false)}
        />
      )}
    </div>
  );
}

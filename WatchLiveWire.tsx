'use client';
import Hls from 'hls.js';
import React, { useState, useEffect, useRef } from 'react';

export function StreamPlayer({ streamUrl }: { streamUrl: string }) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const isHlsStream = Boolean(
    streamUrl &&
      (/\.m3u8(\?|$)/i.test(streamUrl) || streamUrl.toLowerCase().includes('m3u8'))
  );

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !streamUrl || !isHlsStream) return;

    let hls: Hls | null = null;
    // HLS.js for standard browsers
    if (Hls.isSupported()) {
      hls = new Hls({
        enableWorker: true,
        lowLatencyMode: true,
      });
      hls.loadSource(streamUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => {});
      });
    }
    // Native fallback for iOS/Safari
    else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = streamUrl;
      video.play().catch(() => {});
    }

    // CRITICAL FIX: This cleanup function destroys the old stream when you switch feeds
    return () => {
      if (hls) {
        hls.destroy();
      }
      if (video) {
        video.removeAttribute('src');
        video.load();
      }
    };
  }, [streamUrl, isHlsStream]);

  if (!streamUrl) {
    return (
      <div className="w-full h-full bg-black flex items-center justify-center text-xs text-gray-400">
        Select a live feed to start streaming
      </div>
    );
  }

  if (!isHlsStream) {
    return (
      <iframe
        key={streamUrl}
        src={streamUrl}
        className="w-full h-full border-0 bg-black"
        allow="accelerometer *; autoplay *; clipboard-write *; encrypted-media *; gyroscope *; picture-in-picture *; web-share *; fullscreen *"
        allowFullScreen
        referrerPolicy="no-referrer"
      />
    );
  }

  return (
    <video
      ref={videoRef}
      controls
      autoPlay
      muted
      playsInline
      className="w-full h-full bg-black"
    />
  );
}

export default function WatchLiveWire({ streamUrl: propStreamUrl, activeLegs = [] }: { streamUrl?: string; activeLegs?: any[] }) {
  const [streamUrl, setStreamUrl] = useState(propStreamUrl || '');

  useEffect(() => {
    if (typeof window !== 'undefined') {
      const params = new URLSearchParams(window.location.search);
      const queryStream = params.get('streamUrl') || params.get('stream');
      const sport = (params.get('sport') || 'NBA').toUpperCase();
      if (queryStream) {
        setStreamUrl(queryStream);
      } else if (!propStreamUrl) {
        const slug = sport === 'NBA' ? 'nba' : (sport === 'NFL' ? 'nfl' : (sport === 'MLB' ? 'mlb' : 'nba'));
        setStreamUrl(`https://thetvapp.to/${slug}`);
      }
    }
  }, [propStreamUrl]);

  return (
    <div className="w-full min-h-[100dvh] bg-[#0b0e14] text-white flex flex-col font-sans select-none">
      {/* 1. TOP LIVE PLAYER (HLS.js video + unsandboxed embed fallback with direct popout) */}
      <div className="relative w-full aspect-video bg-black sticky top-0 z-20 border-b border-emerald-500/20">
        <StreamPlayer streamUrl={streamUrl} />
        <div className="absolute top-3 left-3 bg-red-600 text-white text-[10px] font-bold px-2 py-0.5 rounded flex items-center gap-1 pointer-events-none">
          <span className="w-1.5 h-1.5 rounded-full bg-white animate-pulse" /> LIVE WIRE
        </div>
        {streamUrl && (
          <div className="absolute top-3 right-3 flex items-center gap-2">
            <a
              href={streamUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="px-2.5 py-1 bg-black/80 backdrop-blur border border-cyan-400 text-cyan-300 text-[10px] font-extrabold rounded-md shadow hover:bg-cyan-500 hover:text-black transition"
            >
              ↗ Open Direct Tab
            </a>
          </div>
        )}
      </div>

      {/* 2. BOTTOM LIVE SWEAT CARDS */}
      <div className="flex-1 p-4 space-y-3 overflow-y-auto z-10">
        <div className="flex items-center justify-between">
          <span className="text-xs font-bold uppercase tracking-wider text-gray-400">Active Auto-Parlay Sweat</span>
          <span className="text-xs font-mono text-emerald-400">⚡ 24/7 Live Wire Active</span>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {(activeLegs.length ? activeLegs : [
            { title: '3-Team Parlay', sub: 'Pistons vs 76ers', odds: '+450', payout: '+$27.50', pct: 78 },
            { title: 'Live Prop Sniper', sub: 'C. Cunningham O 22.5', odds: '+145', payout: '+$11.55', pct: 85 }
          ]).map((card, i) => (
            <div key={i} className="bg-[#151a24] border border-gray-800 rounded-xl p-3 shadow-lg">
              <div className="flex justify-between items-start mb-1">
                <span className="text-xs font-bold text-white">{card.title}</span>
                <span className="text-xs font-extrabold text-emerald-400">{card.payout}</span>
              </div>
              <p className="text-[11px] text-gray-400 mb-3">{card.sub}</p>
              <div className="flex justify-between items-center mb-1.5">
                <span className="text-[10px] font-mono text-gray-300">ODDS: {card.odds}</span>
                <span className="bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 text-[10px] font-extrabold px-2 py-0.5 rounded">
                  WINNING
                </span>
              </div>
              <div className="w-full h-1.5 bg-gray-800 rounded-full overflow-hidden">
                <div className="h-full bg-emerald-500 rounded-full" style={{ width: `${card.pct}%` }} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

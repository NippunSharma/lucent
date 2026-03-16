import React, { useRef, useEffect, useCallback } from 'react';
import { cn } from '@/lib/utils';

const COLORS = {
  idle: { h: 260, s: 60, l: 50 },
  listening: { h: 200, s: 80, l: 55 },
  speaking: { h: 270, s: 90, l: 60 },
};

export default function AudioVisualizer({ analyser, state = 'idle', className }) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const animFrameRef = useRef(null);
  const phaseRef = useRef(0);
  const targetAmplitudeRef = useRef(0);
  const currentAmplitudeRef = useRef(0);
  const sizeRef = useRef({ w: 300, h: 300 });

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width, height } = entry.contentRect;
        sizeRef.current = { w: width, h: height };
        const canvas = canvasRef.current;
        if (canvas) {
          const dpr = window.devicePixelRatio || 1;
          canvas.width = width * dpr;
          canvas.height = height * dpr;
        }
      }
    });

    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    const cx = w / 2;
    const cy = h / 2;
    const minDim = Math.min(sizeRef.current.w, sizeRef.current.h);

    ctx.clearRect(0, 0, w, h);

    let amplitude = 0;
    if (analyser) {
      const data = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(data);
      const sum = data.reduce((a, b) => a + b, 0);
      amplitude = sum / data.length / 255;
    }

    targetAmplitudeRef.current = amplitude;
    currentAmplitudeRef.current += (targetAmplitudeRef.current - currentAmplitudeRef.current) * 0.12;
    const amp = currentAmplitudeRef.current;

    phaseRef.current += 0.015;
    const phase = phaseRef.current;

    const colorSet = COLORS[state] || COLORS.idle;
    const baseRadius = minDim * 0.14;

    for (let ring = 4; ring >= 0; ring--) {
      const ringFactor = ring / 4;
      const breathe = Math.sin(phase * (0.8 + ring * 0.3)) * 0.15;
      const audioScale = 1 + amp * (0.6 + ringFactor * 1.5) + breathe;
      const radius = baseRadius * (1 + ringFactor * 0.7) * audioScale;

      const alpha = 0.08 + ringFactor * 0.04 + amp * 0.1;
      const lightness = colorSet.l + ring * 3 + amp * 15;
      const saturation = colorSet.s + amp * 20;

      ctx.beginPath();
      const points = 120;
      for (let i = 0; i <= points; i++) {
        const angle = (i / points) * Math.PI * 2;
        const noise =
          Math.sin(angle * 3 + phase * (1 + ring * 0.5)) * (4 + amp * 20) +
          Math.sin(angle * 5 - phase * 1.3) * (2 + amp * 12) +
          Math.sin(angle * 7 + phase * 0.7 + ring) * (1 + amp * 8);
        const r = radius + noise;
        const x = cx + Math.cos(angle) * r;
        const y = cy + Math.sin(angle) * r;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();

      const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius * 1.5);
      grad.addColorStop(0, `hsla(${colorSet.h}, ${saturation}%, ${lightness + 15}%, ${alpha * 1.5})`);
      grad.addColorStop(0.6, `hsla(${colorSet.h + 20}, ${saturation - 10}%, ${lightness}%, ${alpha})`);
      grad.addColorStop(1, `hsla(${colorSet.h + 40}, ${saturation - 20}%, ${lightness - 10}%, 0)`);

      ctx.fillStyle = grad;
      ctx.fill();
    }

    const coreGlow = ctx.createRadialGradient(cx, cy, 0, cx, cy, baseRadius * 0.8);
    const coreAlpha = 0.3 + amp * 0.5;
    coreGlow.addColorStop(0, `hsla(${colorSet.h}, 100%, 85%, ${coreAlpha})`);
    coreGlow.addColorStop(0.5, `hsla(${colorSet.h + 10}, 90%, 70%, ${coreAlpha * 0.4})`);
    coreGlow.addColorStop(1, `hsla(${colorSet.h + 20}, 80%, 60%, 0)`);
    ctx.beginPath();
    ctx.arc(cx, cy, baseRadius * 0.8, 0, Math.PI * 2);
    ctx.fillStyle = coreGlow;
    ctx.fill();

    animFrameRef.current = requestAnimationFrame(draw);
  }, [analyser, state]);

  useEffect(() => {
    animFrameRef.current = requestAnimationFrame(draw);
    return () => {
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, [draw]);

  return (
    <div ref={containerRef} className={cn('w-full h-full', className)}>
      <canvas
        ref={canvasRef}
        className="pointer-events-none w-full h-full"
      />
    </div>
  );
}

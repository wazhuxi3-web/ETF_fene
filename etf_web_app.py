from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from etf_database import DEFAULT_DB_PATH, ETFDatabase


def parse_web_endpoint(host: str, port: str | int) -> tuple[str, int]:
    host = str(host or "").strip()
    if not host:
        raise ValueError("网页地址不能为空")
    try:
        port_num = int(str(port).strip())
    except ValueError as exc:
        raise ValueError("端口必须是数字") from exc
    if not 0 <= port_num <= 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    return host, port_num


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ETF份额曲线</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #f6f7f9; color: #20242a; }
    header { padding: 16px 22px; background: #ffffff; border-bottom: 1px solid #dde1e7; display: flex; align-items: center; justify-content: space-between; gap: 14px; }
    h1 { margin: 0; font-size: 20px; }
    main { display: grid; grid-template-columns: 340px 1fr; gap: 14px; padding: 14px; height: calc(100vh - 64px); }
    aside, section { background: #ffffff; border: 1px solid #dde1e7; border-radius: 6px; min-height: 0; }
    aside { display: flex; flex-direction: column; }
    .controls { padding: 12px; display: grid; gap: 10px; border-bottom: 1px solid #e6e9ef; }
    .view-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
    .view-tabs button { height: 30px; padding: 0; background: #eef2f7; color: #26313d; border-color: #cbd2dc; }
    .view-tabs button.active { background: #1f6feb; color: #fff; border-color: #1f6feb; }
    input, select, button { height: 34px; border: 1px solid #cbd2dc; border-radius: 4px; padding: 0 10px; background: #fff; font: inherit; }
    button { cursor: pointer; background: #1f6feb; color: #fff; border-color: #1f6feb; }
    .list { overflow: auto; padding: 8px; }
    .item { display: grid; grid-template-columns: 22px minmax(0, 1fr) 126px 30px; gap: 8px; align-items: center; padding: 8px; border-radius: 4px; }
    .fav { height: 26px; width: 28px; padding: 0; background: #fff; color: #8a94a3; border-color: #cbd2dc; font-size: 15px; }
    .fav.active { color: #d97706; background: #fff7ed; border-color: #f59e0b; }
    .item:hover { background: #f1f4f8; }
    .code { font-weight: 700; }
    .exchange { font-size: 11px; color: #2563eb; }
    .name { font-size: 12px; color: #59636f; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 180px; }
    .share { font-family: Consolas, "Courier New", monospace; font-variant-numeric: tabular-nums; font-feature-settings: "tnum"; font-size: 12px; text-align: right; white-space: nowrap; }
    .chart-wrap { height: 100%; display: grid; grid-template-rows: auto 68px minmax(180px, 2fr) 110px minmax(160px, 1.4fr) 140px; gap: 8px; padding: 12px; }
    .chart-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .chart-tools button { width: 34px; height: 30px; padding: 0; background: #eef2f7; color: #26313d; border-color: #cbd2dc; font-size: 16px; }
    .chart-tools .text-btn { width: auto; padding: 0 10px; font-size: 13px; }
    .chart-tools .text-btn.active { background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; }
    .chart-tools input[type="range"] { flex: 1; min-width: 220px; height: 30px; padding: 0; }
    .range-label { min-width: 210px; color: #59636f; font-size: 13px; }
    .hover-info { min-height: 68px; padding: 7px 10px; border: 1px solid #e1e5eb; border-radius: 4px; background: #fbfcfe; color: #20242a; font-size: 13px; line-height: 1.45; overflow: hidden; display: grid; align-content: center; gap: 3px; }
    .hover-row { display: flex; align-items: baseline; gap: 10px; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .hover-label { flex: 0 0 58px; font-weight: 700; color: #334155; }
    .hover-kline { color: #111827; }
    .hover-share { color: #b91c1c; }
    canvas { width: 100%; height: 100%; border: 1px solid #e1e5eb; border-radius: 4px; }
    .hint { color: #697386; font-size: 13px; }
    .chart-hidden { display: none; }
    @media (max-width: 900px) { main { grid-template-columns: 1fr; height: auto; } aside { height: 420px; } .chart-wrap { height: 780px; } }
  </style>
</head>
<body>
  <header>
    <h1>ETF份额曲线</h1>
    <div id="stats" class="hint"></div>
  </header>
  <main>
    <aside>
      <div class="controls">
        <div class="view-tabs">
          <button id="allView" class="active">全部</button>
          <button id="favView">收藏</button>
        </div>
        <input id="search" placeholder="模糊搜索代码或名称">
        <select id="sort">
          <option value="share_desc">按份额从大到小</option>
          <option value="share_asc">按份额从小到大</option>
          <option value="code">按代码排序</option>
          <option value="name">按名称排序</option>
        </select>
        <button id="clear">清空选择</button>
      </div>
      <div id="list" class="list"></div>
    </aside>
    <section class="chart-wrap">
      <div class="chart-tools">
        <button id="zoomIn" title="放大">⌕+</button>
        <button id="zoomOut" title="缩小">⌕-</button>
        <button id="fullRange" class="text-btn" title="显示全部历史">全周期</button>
        <button id="latestRange" class="text-btn" title="回到最近240日">最近</button>
        <button id="toggleKline" class="text-btn" title="切换线图和K线">K线</button>
        <button id="toggleVolume" class="text-btn" title="隐藏或显示成交量">隐藏成交量</button>
        <button id="togglePrice" class="text-btn" title="隐藏或显示股价线">隐藏股价</button>
        <button id="toggleShare" class="text-btn" title="隐藏或显示份额线">隐藏份额</button>
        <input id="rangeSlider" type="range" min="0" max="0" value="0" title="拖动查看周期">
        <span id="rangeLabel" class="range-label"></span>
      </div>
      <div id="hoverInfo" class="hover-info"></div>
      <canvas id="price"></canvas>
      <canvas id="volume"></canvas>
      <canvas id="shareLine"></canvas>
      <canvas id="delta"></canvas>
    </section>
  </main>
  <script>
    const colors = ['#d62728','#1f77b4','#2ca02c','#9467bd','#ff7f0e','#17becf','#8c564b','#e377c2','#4b5563','#bcbd22'];
    const priceColors = ['#111827','#0f766e','#7c2d12','#4338ca','#be123c','#0369a1','#365314','#86198f','#92400e','#155e75'];
    let selected = new Set();
    let etfs = [];
    let favorites = new Set(JSON.parse(localStorage.getItem('etfFavorites') || '[]'));
    let viewMode = 'all';
    let rawRows = [];
    let visibleRows = [];
    let visibleDates = [];
    let visibleByCode = {};
    let visibleCount = 240;
    let windowStart = 0;
    let showShare = true;
    let showPrice = true;
    let showVolume = true;
    let priceMode = 'line';
    let hoverIndex = null;
    let hoverSource = 'price';
    const $ = id => document.getElementById(id);
    const PADS = {
      price: {l: 72, r: 24, t: 18, b: 22},
      volume: {l: 72, r: 24, t: 14, b: 18},
      share: {l: 72, r: 24, t: 18, b: 24},
      delta: {l: 72, r: 24, t: 16, b: 44},
    };

    async function getJSON(url) {
      const res = await fetch(url);
      return await res.json();
    }

    function money(n) { return Number(n || 0).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
    function priceText(n) { return Number(n || 0).toLocaleString('zh-CN', {minimumFractionDigits: 3, maximumFractionDigits: 3}); }
    function compact(n) { return Number(n || 0).toLocaleString('zh-CN', {maximumFractionDigits: 0}); }
    function sortEtfs(rows, sort) {
      const copy = [...rows];
      if (sort === 'share_asc') return copy.sort((a, b) => a.total_share - b.total_share);
      if (sort === 'code') return copy.sort((a, b) => String(a.fund_code).localeCompare(String(b.fund_code)));
      if (sort === 'name') return copy.sort((a, b) => String(a.fund_name).localeCompare(String(b.fund_name), 'zh-CN'));
      return copy.sort((a, b) => b.total_share - a.total_share);
    }

    async function loadList() {
      const q = encodeURIComponent($('search').value.trim());
      const sort = viewMode === 'favorites' ? 'share_desc' : $('sort').value;
      const rows = await getJSON(`/api/etfs?q=${q}&sort=${sort}`);
      etfs = viewMode === 'favorites'
        ? sortEtfs(rows.filter(e => favorites.has(e.fund_code)), 'share_desc')
        : sortEtfs(rows, sort);
      $('list').innerHTML = etfs.map(e => `
        <label class="item">
          <input type="checkbox" value="${e.fund_code}" ${selected.has(e.fund_code) ? 'checked' : ''}>
          <span><span class="code">${e.fund_code}</span><br><span class="exchange">${e.exchange === 'SZSE' ? '深交所' : '上交所'}</span> <span class="name">${e.fund_name}</span></span>
          <span class="share">${money(e.total_share)}</span>
          <button type="button" class="fav ${favorites.has(e.fund_code) ? 'active' : ''}" data-code="${e.fund_code}" title="收藏">★</button>
        </label>`).join('');
      document.querySelectorAll('.item input').forEach(input => {
        input.addEventListener('change', () => {
          input.checked ? selected.add(input.value) : selected.delete(input.value);
          draw();
        });
      });
      document.querySelectorAll('.fav').forEach(btn => {
        btn.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          const code = btn.dataset.code;
          favorites.has(code) ? favorites.delete(code) : favorites.add(code);
          localStorage.setItem('etfFavorites', JSON.stringify([...favorites]));
          loadList();
        });
      });
      draw();
    }

    function setupCanvas(canvas) {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(300, rect.width * dpr);
      canvas.height = Math.max(80, rect.height * dpr);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return {ctx, w: rect.width, h: rect.height};
    }

    function drawAxes(ctx, w, h, pad, title) {
      ctx.strokeStyle = '#d6dbe3';
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.moveTo(pad.l, pad.t);
      ctx.lineTo(pad.l, h - pad.b);
      ctx.lineTo(w - pad.r, h - pad.b);
      ctx.stroke();
      ctx.fillStyle = '#59636f';
      ctx.font = '11px "Microsoft YaHei", Arial, sans-serif';
      ctx.textAlign = 'left';
      ctx.fillText(title, pad.l + 6, pad.t + 12);
    }

    function xPos(i, dates, w, pad) {
      return pad.l + (dates.length === 1 ? 0 : i * (w - pad.l - pad.r) / (dates.length - 1));
    }

    function yPos(value, min, max, h, pad) {
      const span = max - min || 1;
      return h - pad.b - ((value - min) / span) * (h - pad.t - pad.b);
    }

    function drawDateTicks(ctx, w, h, pad, dates, labels = false) {
      if (!dates.length) return;
      const maxTicks = Math.max(2, Math.floor((w - pad.l - pad.r) / 95));
      const step = Math.max(1, Math.ceil(dates.length / maxTicks));
      const indexes = [];
      for (let i = 0; i < dates.length; i += step) indexes.push(i);
      if (indexes[indexes.length - 1] !== dates.length - 1) indexes.push(dates.length - 1);
      ctx.save();
      ctx.strokeStyle = '#e5e9f0';
      ctx.fillStyle = '#59636f';
      ctx.font = '11px "Microsoft YaHei", Arial, sans-serif';
      ctx.textAlign = 'right';
      indexes.forEach(i => {
        const x = xPos(i, dates, w, pad);
        ctx.beginPath();
        ctx.moveTo(x, h - pad.b);
        ctx.lineTo(x, h - pad.b + 5);
        ctx.stroke();
        if (labels) {
          ctx.save();
          ctx.translate(x - 4, h - pad.b + 22);
          ctx.rotate(-Math.PI / 6);
          ctx.fillText(dates[i], 0, 0);
          ctx.restore();
        }
      });
      ctx.restore();
    }

    async function draw() {
      const codes = [...selected];
      ['price', 'volume', 'shareLine', 'delta'].forEach(id => {
        const box = setupCanvas($(id));
        box.ctx.clearRect(0, 0, box.w, box.h);
      });
      if (!codes.length) return;
      rawRows = await getJSON(`/api/history?codes=${codes.join(',')}`);
      hoverIndex = null;
      resetWindowToLatest();
      renderCharts();
    }

    function allDates(rows) {
      return [...new Set(rows.map(r => r.trade_date))].sort();
    }

    function resetWindowToLatest() {
      const dates = allDates(rawRows);
      visibleCount = Math.min(Math.max(60, Math.min(240, dates.length)), dates.length || 1);
      windowStart = Math.max(0, dates.length - visibleCount);
      updateSlider(dates);
    }

    function showFullRange() {
      const dates = allDates(rawRows);
      visibleCount = dates.length || 1;
      windowStart = 0;
      updateSlider(dates);
      renderCharts();
    }

    function updateVisibilityButtons() {
      $('toggleShare').textContent = showShare ? '隐藏份额' : '显示份额';
      $('togglePrice').textContent = showPrice ? '隐藏股价' : '显示股价';
      $('toggleVolume').textContent = showVolume ? '隐藏成交量' : '显示成交量';
      $('toggleKline').textContent = priceMode === 'line' ? 'K线' : '线图';
      $('toggleShare').classList.toggle('active', !showShare);
      $('togglePrice').classList.toggle('active', !showPrice);
      $('toggleVolume').classList.toggle('active', !showVolume);
      $('toggleKline').classList.toggle('active', priceMode === 'kline');
      $('price').classList.toggle('chart-hidden', !showPrice);
      $('volume').classList.toggle('chart-hidden', !showVolume);
      $('shareLine').classList.toggle('chart-hidden', !showShare);
      document.querySelector('.chart-wrap').style.gridTemplateRows = [
        'auto',
        '68px',
        showPrice ? 'minmax(180px, 2fr)' : null,
        showVolume ? '110px' : null,
        showShare ? 'minmax(160px, 1.4fr)' : null,
        '140px',
      ].filter(Boolean).join(' ');
    }

    function updateSlider(dates) {
      const maxStart = Math.max(0, dates.length - visibleCount);
      windowStart = Math.max(0, Math.min(windowStart, maxStart));
      $('rangeSlider').max = String(maxStart);
      $('rangeSlider').value = String(windowStart);
      const visible = dates.slice(windowStart, windowStart + visibleCount);
      $('rangeLabel').textContent = visible.length ? `${visible[0]} ~ ${visible[visible.length - 1]}（${visible.length}日）` : '';
    }

    function filterRowsByWindow(rows) {
      if (!rows.length) return rows;
      const dates = allDates(rows);
      updateSlider(dates);
      const visible = dates.slice(windowStart, windowStart + visibleCount);
      return rows.filter(r => r.trade_date >= visible[0] && r.trade_date <= visible[visible.length - 1]);
    }

    function groupRows(rows) {
      const byCode = {};
      rows.forEach(r => (byCode[r.fund_code] ||= []).push(r));
      return byCode;
    }

    function priceRange(rows) {
      const values = [];
      rows.forEach(r => {
        if (priceMode === 'kline') {
          if (r.high_price != null) values.push(r.high_price);
          if (r.low_price != null) values.push(r.low_price);
        } else if (r.close_price != null) {
          values.push(r.close_price);
        }
      });
      return values.length ? [Math.min(...values), Math.max(...values)] : [0, 1];
    }

    function candleColor(row, previousClose) {
      if (row.close_price > row.open_price) return '#d62728';
      if (row.close_price < row.open_price) return '#2ca02c';
      return row.close_price >= (previousClose ?? row.open_price) ? '#d62728' : '#2ca02c';
    }

    function drawCandle(ctx, x, width, row, previousClose, min, max, h, pad) {
      const color = candleColor(row, previousClose);
      const highY = yPos(row.high_price, min, max, h, pad);
      const lowY = yPos(row.low_price, min, max, h, pad);
      const openY = yPos(row.open_price, min, max, h, pad);
      const closeY = yPos(row.close_price, min, max, h, pad);
      const top = Math.min(openY, closeY);
      const bottom = Math.max(openY, closeY);
      const bodyHeight = bottom - top;

      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, highY);
      ctx.lineTo(x, lowY);
      ctx.stroke();

      if (bodyHeight < 1) {
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x - width / 2, openY);
        ctx.lineTo(x + width / 2, openY);
        ctx.stroke();
        ctx.lineWidth = 1;
        return;
      }

      const visibleHeight = Math.max(3, bodyHeight);
      const y = top + (bodyHeight - visibleHeight) / 2;
      ctx.fillRect(x - width / 2, y, width, visibleHeight);
    }

    function renderCharts() {
      updateVisibilityButtons();
      const rows = filterRowsByWindow(rawRows);
      const codes = [...selected];
      const byCode = groupRows(rows);
      const dates = [...new Set(rows.map(r => r.trade_date))].sort();
      visibleRows = rows;
      visibleDates = dates;
      visibleByCode = byCode;
      $('stats').textContent = `${codes.length} 个ETF，${dates[0] || '-'} 至 ${dates[dates.length - 1] || '-'}`;

      const price = setupCanvas($('price'));
      const volume = setupCanvas($('volume'));
      const share = setupCanvas($('shareLine'));
      const delta = setupCanvas($('delta'));
      [price, volume, share, delta].forEach(box => box.ctx.clearRect(0, 0, box.w, box.h));
      if (showPrice) drawPriceChart(price.ctx, price.w, price.h, dates, byCode, codes);
      if (showVolume) drawVolumeChart(volume.ctx, volume.w, volume.h, dates, byCode, codes[0]);
      if (showShare) drawShareChart(share.ctx, share.w, share.h, dates, byCode, codes);
      drawDeltaChart(delta.ctx, delta.w, delta.h, dates, byCode, codes[0]);
      if (hoverIndex != null) drawLinkedHover({price, volume, share, delta}, dates, byCode, codes);
    }

    function drawPriceChart(ctx, w, h, dates, byCode, codes) {
      const pad = PADS.price;
      drawAxes(ctx, w, h, pad, priceMode === 'kline' ? '股价K线（红涨绿跌）' : '收盘价线');
      drawDateTicks(ctx, w, h, pad, dates);
      const [min, max] = priceRange(codes.flatMap(code => byCode[code] || []));
      codes.forEach((code, idx) => {
        const rows = byCode[code] || [];
        const map = new Map(rows.map(r => [r.trade_date, r]));
        if (priceMode === 'kline' && idx === 0) {
          const bw = Math.max(3, Math.min(12, (w - pad.l - pad.r) / Math.max(1, dates.length) * 0.65));
          let previousClose = null;
          dates.forEach((d, i) => {
            const r = map.get(d);
            if (!r || r.open_price == null || r.high_price == null || r.low_price == null || r.close_price == null) return;
            const x = xPos(i, dates, w, pad);
            drawCandle(ctx, x, bw, r, previousClose, min, max, h, pad);
            previousClose = r.close_price;
          });
        } else {
          ctx.strokeStyle = priceColors[idx % priceColors.length];
          ctx.lineWidth = 2;
          ctx.beginPath();
          let started = false;
          dates.forEach((d, i) => {
            const r = map.get(d);
            if (!r || r.close_price == null) return;
            const x = xPos(i, dates, w, pad);
            const y = yPos(r.close_price, min, max, h, pad);
            started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
            started = true;
          });
          ctx.stroke();
        }
        const name = (etfs.find(e => e.fund_code === code) || {}).fund_name || code;
        ctx.fillStyle = priceColors[idx % priceColors.length];
        ctx.fillText(`${code} ${name}`, pad.l + 8, pad.t + 28 + idx * 16);
      });
    }

    function drawVolumeChart(ctx, w, h, dates, byCode, code) {
      const pad = PADS.volume;
      drawAxes(ctx, w, h, pad, `${code || ''} 成交量`);
      const rows = byCode[code] || [];
      const map = new Map(rows.map(r => [r.trade_date, r]));
      const maxVol = Math.max(1, ...dates.map(d => (map.get(d) || {}).volume || 0));
      const bw = Math.max(2, (w - pad.l - pad.r) / Math.max(1, dates.length) * 0.72);
      dates.forEach((d, i) => {
        const r = map.get(d);
        if (!r || r.volume == null) return;
        const x = xPos(i, dates, w, pad) - bw / 2;
        const bh = r.volume / maxVol * (h - pad.t - pad.b - 4);
        ctx.fillStyle = (r.close_price || 0) >= (r.open_price || r.close_price || 0) ? '#d62728' : '#2ca02c';
        ctx.fillRect(x, h - pad.b - bh, bw, Math.max(1, bh));
      });
    }

    function drawShareChart(ctx, w, h, dates, byCode, codes) {
      const pad = PADS.share;
      drawAxes(ctx, w, h, pad, '份额线');
      drawDateTicks(ctx, w, h, pad, dates);
      const values = [];
      codes.forEach(c => (byCode[c] || []).forEach(r => values.push(r.total_share)));
      if (!values.length) return;
      const min = Math.min(...values), max = Math.max(...values);
      codes.forEach((code, idx) => {
        const map = new Map((byCode[code] || []).map(r => [r.trade_date, r.total_share]));
        ctx.strokeStyle = colors[idx % colors.length];
        ctx.lineWidth = 2;
        ctx.beginPath();
        let started = false;
        dates.forEach((d, i) => {
          const v = map.get(d);
          if (v == null) return;
          const x = xPos(i, dates, w, pad);
          const y = yPos(v, min, max, h, pad);
          started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
          started = true;
        });
        ctx.stroke();
        const name = (etfs.find(e => e.fund_code === code) || {}).fund_name || code;
        ctx.fillStyle = colors[idx % colors.length];
        ctx.fillText(`${code} ${name}`, pad.l + 8, pad.t + 28 + idx * 16);
      });
    }

    function drawDeltaChart(ctx, w, h, dates, byCode, code) {
      const pad = PADS.delta;
      drawAxes(ctx, w, h, pad, `${code || ''} 份额差量：红=增加，绿=降低`);
      drawDateTicks(ctx, w, h, pad, dates, true);
      const map = new Map((byCode[code] || []).map(r => [r.trade_date, r.share_delta || 0]));
      const maxAbs = Math.max(1, ...dates.map(d => Math.abs(map.get(d) || 0)));
      const zero = pad.t + (h - pad.t - pad.b) / 2;
      ctx.strokeStyle = '#9aa4b2';
      ctx.beginPath();
      ctx.moveTo(pad.l, zero);
      ctx.lineTo(w - pad.r, zero);
      ctx.stroke();
      const bw = Math.max(2, (w - pad.l - pad.r) / Math.max(1, dates.length) * 0.72);
      dates.forEach((d, i) => {
        const v = map.get(d) || 0;
        const x = xPos(i, dates, w, pad) - bw / 2;
        const bh = Math.abs(v) / maxAbs * ((h - pad.t - pad.b) / 2 - 4);
        ctx.fillStyle = v >= 0 ? '#d62728' : '#2ca02c';
        ctx.fillRect(x, v >= 0 ? zero - bh : zero, bw, Math.max(1, bh));
      });
    }

    function nearestIndexFromEvent(event, canvas, dates, pad) {
      if (!dates.length) return null;
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const plotW = rect.width - pad.l - pad.r;
      if (x < pad.l || x > rect.width - pad.r) return null;
      return Math.max(0, Math.min(dates.length - 1, Math.round(((x - pad.l) / plotW) * (dates.length - 1))));
    }

    function drawVertical(ctx, w, h, pad, dates, idx) {
      const x = xPos(idx, dates, w, pad);
      ctx.save();
      ctx.strokeStyle = '#6b7280';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(x, pad.t);
      ctx.lineTo(x, h - pad.b);
      ctx.stroke();
      ctx.restore();
      return x;
    }

    function drawLinkedHover(boxes, dates, byCode, codes) {
      if (hoverIndex == null || !dates.length) return;
      const idx = Math.max(0, Math.min(hoverIndex, dates.length - 1));
      const date = dates[idx];
      const active = hoverSource;
      if (showPrice) drawPriceHover(boxes.price, dates, byCode, codes, idx, active === 'price');
      if (showVolume) drawVolumeHover(boxes.volume, dates, byCode, codes[0], idx, active === 'volume');
      if (showShare) drawShareHover(boxes.share, dates, byCode, codes, idx, active === 'share');
      drawDeltaHover(boxes.delta, dates, byCode, codes[0], idx, active === 'delta');
      updateHoverInfo(date, byCode, codes);
    }

    function drawPriceHover(box, dates, byCode, codes, idx, active) {
      const {ctx, w, h} = box, pad = PADS.price;
      const x = drawVertical(ctx, w, h, pad, dates, idx);
      const [min, max] = priceRange(codes.flatMap(code => byCode[code] || []));
      codes.forEach((code, i) => {
        const row = (byCode[code] || []).find(r => r.trade_date === dates[idx]);
        if (!row || row.close_price == null) return;
        const y = yPos(row.close_price, min, max, h, pad);
        ctx.strokeStyle = priceColors[i % priceColors.length];
        ctx.fillStyle = priceColors[i % priceColors.length];
        if (active) {
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(pad.l, y);
          ctx.lineTo(w - pad.r, y);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
      });
    }

    function drawVolumeHover(box, dates, byCode, code, idx, active) {
      const {ctx, w, h} = box, pad = PADS.volume;
      const x = drawVertical(ctx, w, h, pad, dates, idx);
      const row = (byCode[code] || []).find(r => r.trade_date === dates[idx]);
      if (!row || row.volume == null) return;
      const maxVol = Math.max(1, ...(byCode[code] || []).map(r => r.volume || 0));
      const y = yPos(row.volume, 0, maxVol, h, pad);
      if (active) {
        ctx.strokeStyle = '#6b7280';
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.fillStyle = '#111827';
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    }

    function drawShareHover(box, dates, byCode, codes, idx, active) {
      const {ctx, w, h} = box, pad = PADS.share;
      const x = drawVertical(ctx, w, h, pad, dates, idx);
      const values = [];
      codes.forEach(c => (byCode[c] || []).forEach(r => values.push(r.total_share)));
      const min = values.length ? Math.min(...values) : 0;
      const max = values.length ? Math.max(...values) : 1;
      codes.forEach((code, i) => {
        const row = (byCode[code] || []).find(r => r.trade_date === dates[idx]);
        if (!row) return;
        const y = yPos(row.total_share, min, max, h, pad);
        ctx.strokeStyle = colors[i % colors.length];
        ctx.fillStyle = colors[i % colors.length];
        if (active) {
          ctx.setLineDash([3, 3]);
          ctx.beginPath();
          ctx.moveTo(pad.l, y);
          ctx.lineTo(w - pad.r, y);
          ctx.stroke();
          ctx.setLineDash([]);
        }
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
      });
    }

    function drawDeltaHover(box, dates, byCode, code, idx, active) {
      const {ctx, w, h} = box, pad = PADS.delta;
      const x = drawVertical(ctx, w, h, pad, dates, idx);
      const values = (byCode[code] || []).map(r => r.share_delta || 0);
      const maxAbs = Math.max(1, ...values.map(v => Math.abs(v)));
      const row = (byCode[code] || []).find(r => r.trade_date === dates[idx]);
      if (!row) return;
      const y = yPos(row.share_delta || 0, -maxAbs, maxAbs, h, pad);
      if (active) {
        ctx.strokeStyle = '#6b7280';
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.fillStyle = (row.share_delta || 0) >= 0 ? '#d62728' : '#2ca02c';
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    }

    function setHoverInfoRow(className, labelText, valueText) {
      const row = document.createElement('div');
      row.className = `hover-row ${className}`;
      const label = document.createElement('span');
      label.className = 'hover-label';
      label.textContent = labelText;
      const value = document.createElement('span');
      value.textContent = valueText || '-';
      row.append(label, value);
      return row;
    }

    function clearHoverInfo() {
      $('hoverInfo').textContent = '';
    }

    function updateHoverInfo(date, byCode, codes) {
      const klineParts = [];
      const shareParts = [];
      codes.forEach(code => {
        const row = (byCode[code] || []).find(r => r.trade_date === date);
        if (!row) return;
        const name = (etfs.find(e => e.fund_code === code) || {}).fund_name || '';
        const title = `${code} ${name}`;
        if (row.close_price != null) {
          klineParts.push(`${title}  开 ${priceText(row.open_price)} 高 ${priceText(row.high_price)} 低 ${priceText(row.low_price)} 收 ${priceText(row.close_price)} 量 ${compact(row.volume)}`);
        }
        shareParts.push(`${title}  份额 ${money(row.total_share)} 差量 ${money(row.share_delta || 0)}`);
      });
      const info = $('hoverInfo');
      info.textContent = '';
      info.append(
        setHoverInfoRow('hover-kline', 'K线', `${date}  ${klineParts.join('    ')}`),
        setHoverInfoRow('hover-share', 'ETF份额', `${date}  ${shareParts.join('    ')}`)
      );
    }

    function handleHover(event, source) {
      hoverSource = source;
      const pad = source === 'price' ? PADS.price : source === 'volume' ? PADS.volume : source === 'share' ? PADS.share : PADS.delta;
      const canvasId = source === 'share' ? 'shareLine' : source;
      hoverIndex = nearestIndexFromEvent(event, $(canvasId), visibleDates, pad);
      renderCharts();
    }

    $('search').addEventListener('input', () => loadList());
    $('sort').addEventListener('change', () => loadList());
    $('allView').addEventListener('click', () => {
      viewMode = 'all';
      $('allView').classList.add('active');
      $('favView').classList.remove('active');
      loadList();
    });
    $('favView').addEventListener('click', () => {
      viewMode = 'favorites';
      $('favView').classList.add('active');
      $('allView').classList.remove('active');
      loadList();
    });
    $('clear').addEventListener('click', () => {
      selected.clear();
      rawRows = [];
      hoverIndex = null;
      clearHoverInfo();
      loadList();
    });
    $('zoomIn').addEventListener('click', () => {
      const dates = allDates(rawRows);
      const center = windowStart + visibleCount / 2;
      visibleCount = Math.max(20, Math.floor(visibleCount * 0.65));
      windowStart = Math.round(center - visibleCount / 2);
      updateSlider(dates);
      renderCharts();
    });
    $('zoomOut').addEventListener('click', () => {
      const dates = allDates(rawRows);
      const center = windowStart + visibleCount / 2;
      visibleCount = Math.min(dates.length || 1, Math.ceil(visibleCount * 1.55));
      windowStart = Math.round(center - visibleCount / 2);
      updateSlider(dates);
      renderCharts();
    });
    $('fullRange').addEventListener('click', showFullRange);
    $('latestRange').addEventListener('click', () => { resetWindowToLatest(); renderCharts(); });
    $('toggleKline').addEventListener('click', () => {
      priceMode = priceMode === 'line' ? 'kline' : 'line';
      renderCharts();
    });
    $('toggleVolume').addEventListener('click', () => {
      showVolume = !showVolume;
      renderCharts();
    });
    $('togglePrice').addEventListener('click', () => {
      showPrice = !showPrice;
      renderCharts();
    });
    $('toggleShare').addEventListener('click', () => {
      showShare = !showShare;
      renderCharts();
    });
    $('rangeSlider').addEventListener('input', () => {
      windowStart = Number($('rangeSlider').value || 0);
      hoverIndex = null;
      renderCharts();
    });
    [['price','price'], ['volume','volume'], ['shareLine','share'], ['delta','delta']].forEach(([id, source]) => {
      $(id).addEventListener('mousemove', event => handleHover(event, source));
      $(id).addEventListener('mouseleave', () => { hoverIndex = null; clearHoverInfo(); renderCharts(); });
    });
    window.addEventListener('resize', renderCharts);
    updateVisibilityButtons();
    loadList();
  </script>
</body>
</html>
"""


class ETFRequestHandler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB_PATH

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, HTML, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/etfs":
            qs = parse_qs(parsed.query)
            data = ETFDatabase(self.db_path).list_latest_etfs(
                qs.get("q", [""])[0], qs.get("sort", ["share_desc"])[0]
            )
            self._json(data)
            return
        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            codes = qs.get("codes", [""])[0].split(",")
            data = ETFDatabase(self.db_path).get_history(codes)
            self._json(data)
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def log_message(self, format, *args):
        return

    def _json(self, data):
        self._send(200, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _send(self, status, body, content_type):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ETFWebServer:
    def __init__(self, db_path=DEFAULT_DB_PATH, host="127.0.0.1", port=1234):
        self.db_path = db_path
        self.host = host
        self.port = port
        self.server = None
        self.thread = None

    @property
    def url(self):
        return f"http://{self.host}:{self.port}/"

    def configure(self, host, port):
        host, port = parse_web_endpoint(host, port)
        if self.server and (host != self.host or port != self.port):
            self.stop()
        self.host = host
        self.port = port

    def start(self):
        if self.server:
            return self.url
        handler = type("BoundETFRequestHandler", (ETFRequestHandler,), {"db_path": self.db_path})
        self.server = ThreadingHTTPServer((self.host, self.port), handler)
        self.host, self.port = self.server.server_address[:2]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.url

    def stop(self):
        if not self.server:
            return
        server = self.server
        self.server = None
        try:
            server.shutdown()
        finally:
            server.server_close()
            self.thread = None

    def open(self):
        webbrowser.open(self.start())

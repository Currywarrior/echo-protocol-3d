#!/usr/bin/env node
// 從 index.html 擷取實際地圖、碰撞、A* 與敵人 AI，避免模擬邏輯和遊戲分岔。
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const ROOT = path.resolve(__dirname, "..");
const DT = 1 / 60;
const SECONDS = Number(process.env.SIM_SECONDS || 90);
const TICKS = Math.round(SECONDS / DT);
const REV_MIN_PX = 3.3; // 每 0.1 秒至少 10cm 位移，略過貼牆時的亞公尺碰撞微調
const TYPES = ["grunt", "stalker", "lancer", "warden"];
const DM_TYPES = ["grunt", "grunt", "stalker", "lancer", "warden"];
const MAP_IDS = (process.env.SIM_MAPS || "atrium,corridors,plaza,town").split(",");
const SEEDS = (process.env.SIM_SEEDS || process.env.SIM_SEED || "20260920,20260921,20260922,20260923,20260924,20260925,20260926,20260927,20260928,20260929").split(",").map(Number);
const SCENARIOS = (process.env.SIM_SCENARIOS || "fixed,moving").split(",");
const TYPES_FILTER = (process.env.SIM_TYPES || "").split(",").filter(Boolean);

function blockEnd(source, open) {
  let depth = 0, quote = "", line = false, comment = false;
  for (let i = open; i < source.length; i++) {
    const c = source[i], n = source[i + 1];
    if (line) { if (c === "\n") line = false; continue; }
    if (comment) { if (c === "*" && n === "/") { comment = false; i++; } continue; }
    if (quote) {
      if (c === "\\") { i++; continue; }
      if (c === quote) quote = "";
      continue;
    }
    if (c === "/" && n === "/") { line = true; i++; continue; }
    if (c === "/" && n === "*") { comment = true; i++; continue; }
    if (c === "'" || c === "\"" || c === "`") { quote = c; continue; }
    if (c === "{") depth++;
    else if (c === "}" && --depth === 0) return i + 1;
  }
  throw new Error("未找到配對的 JavaScript 大括號");
}

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`找不到 function ${name}`);
  const open = source.indexOf("{", start);
  return source.slice(start, blockEnd(source, open));
}

function extractForLoop(source, marker, loopText) {
  const markerAt = source.indexOf(marker);
  if (markerAt < 0) throw new Error(`找不到 AI 標記：${marker}`);
  const pattern = new RegExp(loopText.split("").map(c => /\s/.test(c) ? "\\s*" : c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(""));
  const match = pattern.exec(source.slice(markerAt));
  const start = match ? markerAt + match.index : -1;
  if (start < 0) throw new Error(`找不到 AI 迴圈：${loopText}`);
  const open = source.indexOf("{", start);
  return source.slice(start, blockEnd(source, open));
}

function findWhitespaceInsensitive(source, value, from = 0) {
  const pattern = new RegExp(value.split("").map(c => /\s/.test(c) ? "\\s*" : c.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(""));
  const match = pattern.exec(source.slice(from));
  return match ? from + match.index : -1;
}

function getParts(source) {
  const mapsStart = findWhitespaceInsensitive(source, "const STORY =");
  const mapsEnd = findWhitespaceInsensitive(source, "let activeMap = MAPS.atrium", mapsStart);
  const typesStart = findWhitespaceInsensitive(source, "const TYPES = {");
  const typesEnd = findWhitespaceInsensitive(source, "function rayHit(", typesStart);
  if (mapsStart < 0 || mapsEnd < 0 || typesStart < 0 || typesEnd < 0) throw new Error(`擷取標記錯誤：${mapsStart}, ${mapsEnd}, ${typesStart}, ${typesEnd}`);
  const mapText = source.slice(mapsStart, mapsEnd);
  const typeText = source.slice(typesStart, typesEnd);
  // VAL 常數（加減速、移動誤差死區）照原始碼；PXM 在模擬裡另外設成 1，這裡要用遊戲裡的真實值（1/S）
  const valStart = findWhitespaceInsensitive(source, "const VAL = {");
  const valText = valStart < 0 ? "" : source.slice(valStart, blockEnd(source, source.indexOf("{", valStart)) + 1);
  // Valorant 敵人戰術（VAL_AI 區段）：舊版沒有就略過
  const aiStart = source.indexOf("// ── VAL_AI 開始"), aiEnd = source.indexOf("// ── VAL_AI 結束");
  const tacticsText = aiStart >= 0 && aiEnd > aiStart ? source.slice(aiStart, aiEnd) : "";
  // 只掃描兩個需要的迴圈；舊版單行 HTML 的其他函式含正規表示式大括號，整段掃描會誤判函式結尾。
  const botsLoop = extractForLoop(source, "function update(", "for (const b of G.bots)");
  const separationLoop = extractForLoop(source, "function update(", "for (let i=0;i<G.bots.length;i++)");
  let instrumentedAI = (botsLoop + "\n" + separationLoop).replace(
    "[tx, ty] = navStep(b, tx, ty, dt);",
    "b._targetX = tx; b._targetY = ty; b._los = los; __activeBot = b; [tx, ty] = navStep(b, tx, ty, dt); b._intentX = tx; b._intentY = ty; __activeBot = null;"
  );
  if (!instrumentedAI.includes("b._intentX")) throw new Error("無法在 AI 迴圈插入目標點量測");
  // keepBand 的條件在不同版本多了戰術旗標（!tac、!aiming），用正規表示式比對整行，前後版本都能插入量測
  instrumentedAI = instrumentedAI.replace(
    /const keepBand = [^;]*;/,
    m => m + " b._simKeepBand=keepBand; b._simStrafe=b.strafe; b._simDodge=b.dodge||0; b._simDist=d; b._simKeepDist=T.keepDist;"
  );
  instrumentedAI = instrumentedAI.replace(
    "sideX = Math.cos(t2); sideY = Math.sin(t2);",
    "sideX = Math.cos(t2); sideY = Math.sin(t2); b._simBothSidesBlocked=keepBand&&!botRouteClear(b,sideX,sideY,90)&&!botRouteClear(b,-sideX,-sideY,90);"
  );
  instrumentedAI = instrumentedAI.replace(
    "if (lineClear(b.x,b.y,altX,altY)) b.strafe *= -1;",
    "const __altClear=lineClear(b.x,b.y,altX,altY), __sideClear=lineClear(b.x,b.y,b.x+ax*80,b.y+ay*80); b._simBothSidesBlocked=!__altClear&&!__sideClear; if (__altClear) b.strafe *= -1;"
  );
  // 刻意站定（架槍、躲掩體、急停開槍）的時間點：這段時間原地不動是故意的，不算卡住（舊版沒有這行就不會插入）
  instrumentedAI = instrumentedAI.replace(
    "if (still){ tx = b.x; ty = b.y; }",
    "if (still){ tx = b.x; ty = b.y; b._simStillT = G.t; }"
  );
  instrumentedAI = instrumentedAI.replace(
    "const bhit = moveWithCollision(b, ax*spd*dt, ay*spd*dt);",
    "b._simAx=ax; b._simAy=ay; b._intentX=tx; b._intentY=ty; const bhit = moveWithCollision(b, ax*spd*dt, ay*spd*dt);"
  ).replace(
    "b.moved = bmoved;",
    "b.moved = bmoved; b._simPreSepX=b.x; b._simPreSepY=b.y;"
  );
  return { mapText, typeText, instrumentedAI, valText, tacticsText };
}

function setupContext(parts, mapId, seed) {
  let rng = seed >>> 0;
  const seededMath = Object.create(Math);
  seededMath.random = () => {
    rng = (Math.imul(rng, 1664525) + 1013904223) >>> 0;
    return rng / 0x100000000;
  };
  const ctx = vm.createContext({
    Math: seededMath, console, Uint8Array, Float32Array, Int32Array,
    W: 1280, H: 720, W0: 1280, H0: 720, S: 0.03, WALL_H: 3.2,
    EYE: 1.62, HU: 0.0254 / 0.03, BOT_SCALE: 0.88, BOT_H: 2.0,
    STEP_UP: 0.45, LEDGE_REACH: 0.32, NAV_CELL: 12, NAV_PAD: 14,
    WALLS: [], SEGS: [], SPAWNS: [], activeMap: null, NAV: null, NAV_CACHE: new WeakMap(),
    G: null, __activeBot: null, __metrics: null, SIM_TRACE:!!process.env.SIM_TRACE,
    buildFootprint: () => {}, addFootprint: () => {},
    sfxNoiseAt: () => {}, sfxAt: () => {}, botShoot: b => ctx.__onShot && ctx.__onShot(b), DEG: Math.PI / 180,
    SET: {aiDiff: process.env.SIM_DIFF || "normal"},
    updateDummy: () => {}, VAL: {tagT: 1}, ENEMY_SPEED_K: (173.5 * (0.0254 / 0.03)) / 215,
  });

  vm.runInContext(parts.mapText, ctx);
  vm.runInContext(parts.typeText, ctx);
  const map = vm.runInContext(`MAPS.${mapId}`, ctx);
  ctx.W = map.size ? map.size.w : 1280;
  ctx.H = map.size ? map.size.h : 720;
  ctx.activeMap = map;
  ctx.WALLS = map.walls.map(w => ({...w}));
  ctx.SPAWNS = map.spawns;
  // useMap 會替非垂直滑索端點加碰撞柱；AI 在 RIDGE TOWN 也會撞到這些柱子。
  if (map.zips && !map.polesAdded) for (const zp of map.zips) {
    if (Math.hypot(zp.b[0] - zp.a[0], zp.b[1] - zp.a[1]) < 2) continue;
    for (const e of [zp.a, zp.b]) {
      if (e[2] < 0.5) continue;
      const pole = {x:e[0]-5, y:e[1]-5, w:10, h:10, ht:e[2]+0.45, pole:true};
      if (e[3]) pole.base = e[3];
      ctx.WALLS.push(pole);
    }
  }
  const segments = [];
  for (const w of ctx.WALLS) if (!w.ht) {
    const {x,y} = w, X=x+w.w, Y=y+w.h;
    segments.push({x1:x,y1:y,x2:X,y2:y},{x1:X,y1:y,x2:X,y2:Y},
      {x1:X,y1:Y,x2:x,y2:Y},{x1:x,y1:Y,x2:x,y2:y});
  }
  ctx.SEGS = segments;

  const funcs = ["rayHit", "hasLOS", "standable", "topAt", "aabbHit", "moveWithCollision",
    "buildNav", "navBlocked", "navNearestFree", "lineClear", "findPath", "navStep", "botK", "eyeH"];
  if (parts.fullSource.includes("function botRouteClear(")) funcs.push("botRouteClear");
  for (const name of funcs) {
    if(name === "navNearestFree" && !parts.fullSource.includes("function navNearestFree(")) continue;
    vm.runInContext(extractFunction(parts.fullSource,name), ctx);
  }
  vm.runInContext("const BODY_H=1.83, CROUCH_H=1.2; function bodyH(){return G && G.crouching ? CROUCH_H : BODY_H;}", ctx);
  vm.runInContext("const PXM = 1;", ctx);
  if (parts.valText) ctx.VAL = vm.runInContext("(() => { const PXM = 1 / 0.03; " + parts.valText + " return VAL; })()", ctx);
  if (parts.tacticsText) vm.runInContext(parts.tacticsText, ctx);
  // 以原始 A* 做同一份網格計數；活躍 AI 呼叫仍會被收集。
  vm.runInContext("function __installPathCounter(){ const raw=findPath; globalThis.__rawFindPath=raw; globalThis.findPath=function(ax,ay,bx,by){ const b=__activeBot; if(b && __metrics){ const id=b._simId, m=__metrics[id]; m.pathCalls++; if(b.path && b.pathGX!==undefined && Math.hypot(b.pathGX-bx,b.pathGY-by)<40 && b.pathI < b.path.length-1 && Math.hypot(b.path[b.pathI].x-b.x,b.path[b.pathI].y-b.y)>40) m.repeatPath++; } const out=raw(ax,ay,bx,by); if(!out && __activeBot && __metrics){ const m=__metrics[__activeBot._simId]; m.emptyPaths++; if(SIM_TRACE&&m.emptyExamples.length<8)m.emptyExamples.push({x:+__activeBot.x.toFixed(1),y:+__activeBot.y.toFixed(1),tx:+bx.toFixed(1),ty:+by.toFixed(1),trackX:+(__activeBot._targetX||0).toFixed(1),trackY:+(__activeBot._targetY||0).toFixed(1),keepGoal:!!__activeBot.keepGoal,pathFail:!!__activeBot.pathFail}); } return out; }; }", ctx);
  vm.runInContext("function runBotAI(dt){ const p=G.player;" + parts.instrumentedAI + "}", ctx);
  ctx.__installPathCounter = vm.runInContext("__installPathCounter", ctx);
  ctx.__installPathCounter();
  vm.runInContext("buildNav();", ctx);
  return ctx;
}

function playerStart(ctx, moving) {
  if(process.env.SIM_PLAYER_POS){const [x,y]=process.env.SIM_PLAYER_POS.split(",").map(Number);return {x,y};}
  if(moving) return ctx.activeMap.player || {x:ctx.W/2,y:ctx.H/2};
  {
    if(ctx.activeMap.name==="RIDGE TOWN") return {x:80,y:1160};
    if(ctx.activeMap.name==="DEAD SIGNAL") return {x:50,y:360};
    return {x:40,y:360};
  }
}

function makeBots(ctx, seed, moving) {
  const sps = ctx.SPAWNS, p = playerStart(ctx,moving);
  const avoid = [p];
  const score = s => {
    let m=1e9, seen=false;
    for (const a of avoid) { m=Math.min(m,Math.hypot(s.x-a.x,s.y-a.y)); if (vm.runInContext(`hasLOS(${a.x},${a.y},${s.x},${s.y})`,ctx)) seen=true; }
    return m-(seen?600:0);
  };
  const bots=[];
  for (let i=0;i<DM_TYPES.length;i++) {
    const ranked=sps.slice().sort((a,b)=>score(b)-score(a));
    const s=ranked[Math.floor(vm.runInContext("Math.random()",ctx)*Math.min(3,ranked.length))];
    vm.runInContext("Math.random()",ctx); // DM_TYPES 的型別抽樣；此模擬固定使用完整陣容
    const type=DM_TYPES[i], T=vm.runInContext(`TYPES.${type}`,ctx);
    const x=s.x+(vm.runInContext("Math.random()",ctx)*30-15), y=s.y+(vm.runInContext("Math.random()",ctx)*30-15);
    const bot={type,x,y,rad:T.rad,hp:T.hp,angle:Math.atan2(p.y-y,p.x-x),cool:0,burst:T.burstSize,
      see:0,lastX:x,lastY:y,goalX:x,goalY:y,strafe:vm.runInContext("Math.random()",ctx)<0.5?1:-1,stuck:0,px:x,py:y,
      revealed:0,flash:0,tagT:0,tagAmt:0,step:0,path:null,pathI:0,pathT:0};
    bot._simId=bots.length; bots.push(bot);
  }
  ctx.G={bots,player:{...p,z:0,rad:13,hp:100},t:0,val:true,mode:"fight",crouching:false,crouchT:0};
  return bots;
}

function playerPosition(ctx, time, moving) {
  if(!moving) return {...playerStart(ctx,false)};
  // 往返同一條路線，持續改變追蹤目標，避免長回合後退化成固定站位。
  const stops=[playerStart(ctx,moving), ...ctx.SPAWNS.slice(0,4)];
  const spots=[...stops, ...stops.slice(1,-1).reverse(), stops[0]];
  const speed=58;
  let remaining=(time*speed)%spots.slice(1).reduce((sum,p,i)=>sum+Math.hypot(p.x-spots[i].x,p.y-spots[i].y),0), pos={...spots[0]};
  for(let i=1;i<spots.length;i++){
    const target=spots[i], d=Math.hypot(target.x-pos.x,target.y-pos.y);
    if(remaining<=d){const t=d?remaining/d:0;return {x:pos.x+(target.x-pos.x)*t,y:pos.y+(target.y-pos.y)*t};}
    remaining-=d; pos={...target};
  }
  return {...spots[0]};
}

function simulate(ctx, mapId, seed, moving) {
  const bots=makeBots(ctx,seed,moving), metrics={};
  for(const b of bots) metrics[b._simId]={pathCalls:0,emptyPaths:0,repeatPath:0,reversals:0,stuckEpisodes:0,examples:[],revExamples:[],emptyExamples:[],
    shots:0,shotSpeed:0,steadyShots:0,engagedT:0,coverT:0,hurts:0};
  ctx.__metrics=metrics;
  // 開槍當下的移動速度（這一幀實際位移，不含敵人互推）；「站定」= 低於自己跑速的 27.5%（Valorant 移動誤差的死區）
  ctx.__onShot=b=>{const m=metrics[b._simId],T=vm.runInContext("TYPES",ctx)[b.type],v=b.moved/DT;m.shots++;m.shotSpeed+=v*0.03;if(v<=T.speed*ctx.ENEMY_SPEED_K*0.275)m.steadyShots++;};
  // 玩家還手：敵人看得到玩家時，平均每暴露 1.5 秒被打中一次（獨立亂數，不影響 AI 自己的亂數序列）
  let hurtRng=(seed^0x5bd1e995)>>>0;const hurtRand=()=>(hurtRng=(Math.imul(hurtRng,1664525)+1013904223)>>>0)/0x100000000;
  const lastLos=bots.map(()=>null);
  const hist=bots.map(b=>[{x:b.x,y:b.y,t:0}]), prevDir=bots.map(()=>null), lastDirTime=bots.map(()=>-10);
  const sampled=bots.map(b=>({x:b.x,y:b.y}));
  const sim=vm.runInContext("runBotAI",ctx);
  let prevPlayer=playerPosition(ctx,0,moving);
  for(let frame=0;frame<TICKS;frame++){
    const time=frame*DT, pp=playerPosition(ctx,time,moving);
    ctx.G.player.vx=(pp.x-prevPlayer.x)/DT;ctx.G.player.vy=(pp.y-prevPlayer.y)/DT;
    ctx.G.player.x=pp.x;ctx.G.player.y=pp.y;ctx.G.t=time;prevPlayer=pp;
    const t0=process.hrtime.bigint();
    sim(DT);
    __tickNs.push(Number(process.hrtime.bigint()-t0));
    for(const b of bots){
      const i=b._simId,m=metrics[i], dx=b.x-b.px,dy=b.y-b.py,dist=Math.hypot(dx,dy);
      if(!process.env.SIM_NO_HURT && b._los && hurtRand()<DT/1.5){b.hurtAt=time;m.hurts++;}
      // 掩體後：最近 3 秒內看過玩家（交火中），這一幀看不到玩家，但離最後看得到玩家的位置不到 3m（隨時能再探頭）
      if(b._los) lastLos[i]={x:b.x,y:b.y,t:time};
      else if(lastLos[i]&&time-lastLos[i].t<3){m.engagedT+=DT;if(Math.hypot(b.x-lastLos[i].x,b.y-lastLos[i].y)*0.03<=3)m.coverT+=DT;}
      if(b._los&&lastLos[i])m.engagedT+=DT;
      b._totalDist=(b._totalDist||0)+dist;
      if(frame%6===5){
        const sx=b.x-sampled[i].x,sy=b.y-sampled[i].y,sd=Math.hypot(sx,sy);
        if(sd>1){
          const now={x:sx/sd,y:sy/sd};
          if(prevDir[i] && prevDir[i].len>=REV_MIN_PX && sd>=REV_MIN_PX && time-lastDirTime[i]<=0.6 && prevDir[i].x*now.x+prevDir[i].y*now.y < Math.cos(150*Math.PI/180)){
            m.reversals++;
            if(process.env.SIM_TRACE&&m.revExamples.length<24)m.revExamples.push({t:+time.toFixed(2),x:+b.x.toFixed(1),y:+b.y.toFixed(1),prev:{x:+prevDir[i].x.toFixed(2),y:+prevDir[i].y.toFixed(2),len:+prevDir[i].len.toFixed(1)},now:{x:+now.x.toFixed(2),y:+now.y.toFixed(2),len:+sd.toFixed(1)},dot:+(prevDir[i].x*now.x+prevDir[i].y*now.y).toFixed(2),stuck:+b.stuck.toFixed(2),dodge:+(b.dodge||0).toFixed(2)});
          }
          prevDir[i]={...now,len:sd};lastDirTime[i]=time;
        }
        sampled[i]={x:b.x,y:b.y};
      }
      const elapsed=time+DT,h=hist[i];h.push({x:b.x,y:b.y,t:elapsed});while(h.length&&elapsed-h[0].t>2.01)h.shift();
      // 以不重疊的 2 秒區間計數，避免同一段卡住因單幀邊界抖動被重複計數數百次。
      if((frame+1)%120===0&&h.length){
        const start=h.find(q=>elapsed-q.t>=2-DT), net=start?Math.hypot(b.x-start.x,b.y-start.y):Infinity;
        // navStep 的回傳值是此刻真正要走的點：有路徑時是路徑點，直線可達時才是追蹤目標。
        const intent=Math.hypot((b._intentX??b.x)-b.x,(b._intentY??b.y)-b.y);
        // 視窗內有刻意站定過（_simStillT）就不算：例如架槍 3 秒後剛起步，2 秒內淨位移自然不到 0.8m。
        // SIM_STUCK_RAW=1 關掉這個排除，用舊定義對照
        const stuck=net<0.8/0.03 && intent>3/0.03 && (!!process.env.SIM_STUCK_RAW || !(elapsed-(b._simStillT??-99)<2));
        if(stuck){
          m.stuckEpisodes++;
        if(process.env.SIM_TRACE&&m.examples.length<8)m.examples.push({t:+elapsed.toFixed(2),netM:+(net*0.03).toFixed(2),targetM:+(intent*0.03).toFixed(2),x:+b.x.toFixed(1),y:+b.y.toFixed(1),preSep:[+b._simPreSepX.toFixed(1),+b._simPreSepY.toFixed(1)],tx:+b._targetX.toFixed(1),ty:+b._targetY.toFixed(1),nx:+b._intentX.toFixed(1),ny:+b._intentY.toFixed(1),move:[+b._simAx.toFixed(2),+b._simAy.toFixed(2)],los:!!b._los,keepBand:!!b._simKeepBand,keepGoal:!!b.keepGoal,distM:+((b._simDist||0)*0.03).toFixed(2),keepDistM:+((b._simKeepDist||0)*0.03).toFixed(2),strafe:b._simStrafe,dodge:+(b._simDodge||0).toFixed(2),bothSidesBlocked:!!b._simBothSidesBlocked,hit:[!!b.hitX,!!b.hitY],stuck:+b.stuck.toFixed(2),pathI:b.pathI,pathN:b.path&&b.path.length,nearestBotM:+(Math.min(...ctx.G.bots.filter(q=>q!==b).map(q=>Math.hypot(q.x-b.x,q.y-b.y)))*0.03).toFixed(2)});
        }
      }
    }
  }
  return bots.map(b=>{const m=metrics[b._simId];return {map:mapId,type:b.type,...m,travelM:(b._totalDist||0)*0.03};});
}

const __tickNs=[];
function runVariant(label, source) {
  const parts=getParts(source);parts.fullSource=source;
  const all=[];
  for(const scenario of SCENARIOS) for(const seed of SEEDS) for(const mapId of MAP_IDS){
    const moving=scenario === "moving";
    const mapSeed=seed+Math.max(0, ["atrium","corridors","plaza","town"].indexOf(mapId))*101+(moving?50000:0);
    const ctx=setupContext(parts,mapId,mapSeed);
    all.push(...simulate(ctx,mapId,mapSeed,moving).map(row=>({...row,scenario,seed})));
  }
  const grouped=new Map();
  for(const row of all){
    const key=`${row.scenario}/${row.map}/${row.type}`;
    if(!grouped.has(key))grouped.set(key,{scenario:row.scenario,map:row.map,type:row.type,seeds:SEEDS.length,bots:0,pathCalls:0,emptyPaths:0,repeatPath:0,reversals:0,stuck2s:0,travelM:0,
      shots:0,shotSpeed:0,steadyShots:0,engagedT:0,coverT:0,examples:[],revExamples:[],emptyExamples:[]});
    const g=grouped.get(key);g.bots++;g.shots+=row.shots;g.shotSpeed+=row.shotSpeed;g.steadyShots+=row.steadyShots;g.engagedT+=row.engagedT;g.coverT+=row.coverT;g.pathCalls+=row.pathCalls;g.emptyPaths+=row.emptyPaths;g.repeatPath+=row.repeatPath;g.reversals+=row.reversals;g.stuck2s+=row.stuckEpisodes;g.travelM+=row.travelM;if(process.env.SIM_TRACE){g.examples.push(...row.examples);g.revExamples.push(...row.revExamples);g.emptyExamples.push(...row.emptyExamples);}
  }
  const data=[...grouped.values()].filter(r=>!TYPES_FILTER.length||TYPES_FILTER.includes(r.type)).map(r=>({...r,travelM:+(r.travelM/r.bots).toFixed(1),
    shotSpeedMs:r.shots?+(r.shotSpeed/r.shots).toFixed(2):null, steadyPct:r.shots?+(100*r.steadyShots/r.shots).toFixed(1):null,
    coverPct:r.engagedT?+(100*r.coverT/r.engagedT).toFixed(1):null}));
  // 每次 AI 更新（五隻敵人的整段迴圈，含分離）的耗時
  const ns=__tickNs.slice().sort((a,b)=>a-b), q=f=>+(ns[Math.min(ns.length-1,Math.floor(ns.length*f))]/1000).toFixed(1);
  const out={label, seconds:SECONDS, seeds:SEEDS, scenarios:SCENARIOS, diff:process.env.SIM_DIFF||"normal", tickUs:{median:q(0.5),p99:q(0.99),max:q(1)}, data};
  return out;
}

function main() {
  const args=new Set(process.argv.slice(2));
  const current=fs.readFileSync(path.join(ROOT,"index.html"),"utf8");
  let source=current,label="目前版本";
  if(args.has("--before-stdin")){source=fs.readFileSync(0,"utf8");label="修正前";}
  else if(args.has("--before-file")){
    const at=process.argv.indexOf("--before-file");source=fs.readFileSync(path.resolve(process.argv[at+1]),"utf8");label="修正前";
  }else if(args.has("--candidate-file")){
    const at=process.argv.indexOf("--candidate-file");source=fs.readFileSync(path.resolve(process.argv[at+1]),"utf8");label="指定候選版";
  }else if(args.has("--after")) label="修正後";
  console.log(JSON.stringify(runVariant(label,source)));
}

main();

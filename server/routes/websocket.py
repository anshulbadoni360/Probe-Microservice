import re
import asyncio
import json
from bson import ObjectId
from types import SimpleNamespace

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from utils.ServerLogger import ServerLogger
from services.ProdProbe_v2 import Probe, NSIGHT_v2
from models.Survey import SurveyResponse, SurveyConfig, QuestionConfig
from utils.helper import Helper

websocket_router = APIRouter(prefix="/ws", tags=["websocket", "ai-qa"])
logger = ServerLogger()


@websocket_router.websocket("/ai-qa")
async def websocket_ai_qa(websocket: WebSocket):
    redis_client = websocket.app.state.redis
    probe_cache = websocket.app.state.probe_cache
    
    await websocket.accept()
    current_key = None
    
    try:
        async for data in websocket.iter_text():
            try:
                survey_response = SurveyResponse.model_validate_json(data)
                
                raw_payload = await redis_client.get(f"survey_details:{survey_response.su_id}:{survey_response.qs_id}")
                if not raw_payload:
                    await websocket.send_json({"error": True, "message": "Survey context not found", "code": 404})
                    continue
                
                survey_payload = json.loads(raw_payload)
                survey_data = survey_payload.get("survey", {})
                question_data = survey_payload.get("question", {})
                
                survey_config = SurveyConfig(
                    language=survey_data.get("language", "English"),
                    add_context=survey_data.get("add_context", True),
                )
                survey = SimpleNamespace(
                    id=survey_response.su_id,
                    description=survey_data.get("survey_description", "-"),
                    config=survey_config,
                )
                
                question_config = QuestionConfig(
                    probes=question_data.get("min_probe", 0),
                    max_probes=question_data.get("max_probe", 0),
                    quality_threshold=question_data.get("quality_threshold", 4),
                    gibberish_score=question_data.get("gibberish_score", 4),
                    relevance_threshold=question_data.get("relevance_threshold", 4),
                    add_context=question_data.get("add_context", True)
                )
                
                question = SimpleNamespace(
                    id=survey_response.qs_id,
                    question=survey_response.question,
                    description=survey_payload.get("description", ""), 
                    config=question_config,
                )
                current_key = f"{survey_response.su_id}-{survey_response.qs_id}-{survey_response.mo_id}"
                session_data = await probe_cache.get(current_key) or {}
                session_no = session_data.get("session_no", 0)
                probe = Probe(
                    mo_id=survey_response.mo_id,
                    metadata=survey,
                    question=question,
                    simple_store=True,
                    session_no=session_no,
                    survey_details=survey_response
                )
                counter = 0
                probe.counter = counter
                
                # Synchronize History Before Parallel Streams
                probe.add_user_response(survey_response.response)

                f_q, n_buf, l_rel, l_m, rel_e = "", [], None, None, asyncio.Event()

                async def send(msg, q_text=None, m_obj=None):
                    res = {"question": q_text, "ended": probe.ended, "min_probing": question_config.probes, "max_probing": question_config.max_probes}
                    if m_obj:
                        d = m_obj.model_dump() if hasattr(m_obj, 'model_dump') else m_obj
                        if isinstance(d, dict) and d:
                            res["metrics"] = d
                            if "gibberish_score" in d: res["is_gibberish"] = d["gibberish_score"] > question_config.gibberish_score
                    await websocket.send_json({"error": False, "message": msg, "code": 200, "response": res})

                async def run_metrics():
                    nonlocal l_m, l_rel
                    try:
                        logger.info(f"[Metrics] History has {len(probe._history.messages)} messages")
                        l_m = await probe.gen_metrics(survey_response.response)
                        if l_m:
                            l_rel = getattr(l_m, 'relevance', None)
                            logger.info(f"[Metrics] Done: l_m={l_m}, l_rel={l_rel}")
                    except Exception as e:
                        logger.error(f"[Metrics] ERROR: {e}")
                    finally:
                        rel_e.set()
                        await send("streaming", m_obj=l_m)

                async def run_natural():
                    nonlocal f_q
                    async for chunk in probe.gen_follow_up_stream():
                        if rel_e.is_set():
                            if l_rel is not None and l_rel < question_config.relevance_threshold: break
                            f_q += chunk.content; await send("streaming", q_text=chunk.content, m_obj=l_m)
                        else: n_buf.append(chunk)

                await send("streaming-started", q_text="")
                m_task, n_task = asyncio.create_task(run_metrics()), asyncio.create_task(run_natural())
                
                try: await rel_e.wait()
                except: pass

                if l_rel is not None and l_rel < question_config.relevance_threshold:
                    n_task.cancel()
                    async for chunk in probe.gen_follow_up_stream(use_redirection=True):
                        f_q += chunk.content; await send("streaming", q_text=chunk.content, m_obj=l_m)
                else:
                    for chunk in n_buf: f_q += chunk.content; await send("streaming", q_text=chunk.content, m_obj=l_m)
                    await n_task

                await m_task
                if l_m and getattr(l_m, 'quality', 0) >= question_config.quality_threshold: probe.ended = True
                await send("streaming-ended", q_text=f_q, m_obj=l_m)
            
            except Exception as e:
                logger.error(f"Error processing request: {e}")
                
                if current_key:
                    await probe_cache.delete(current_key)
                
                await websocket.send_json({
                    "error": True,
                    "message": "Error generating follow-up",
                    "code": 500
                })
    
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass
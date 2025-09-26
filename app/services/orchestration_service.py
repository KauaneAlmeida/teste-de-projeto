"""
Intelligent Orchestration Service

Este serviço orquestra inteligentemente entre IA (Gemini) e fluxo estruturado (Firebase).
Corrigido para avançar corretamente no fluxo de perguntas.
"""

import logging
import re
import uuid
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

from app.services.ai_chain import ai_orchestrator
from app.services.firebase_service import (
    get_conversation_flow,
    save_user_session,
    get_user_session,
    save_lead_data,
    update_lead_data
)
from app.services.baileys_service import baileys_service
from app.services.lawyer_notification_service import lawyer_notification_service

logger = logging.getLogger(__name__)

class IntelligentHybridOrchestrator:
    """
    Orquestrador inteligente que gerencia conversas entre IA e fluxo estruturado.
    Corrigido para avançar corretamente no fluxo de perguntas.
    """

    def __init__(self):
        self.gemini_unavailable_until = None
        self.gemini_check_interval = timedelta(minutes=5)

    async def process_message(
        self,
        message: str,
        session_id: str,
        phone_number: str = None,
        platform: str = "web"
    ) -> Dict[str, Any]:
        """
        Processa mensagem com lógica corrigida de avanço no fluxo.
        """
        try:
            logger.info(f"🎯 Processando mensagem | session={session_id} | platform={platform} | msg='{message[:50]}...'")

            # Buscar ou criar sessão
            session_data = await get_user_session(session_id)
            if not session_data:
                session_data = await self._create_new_session(session_id, platform)

            # Verificar se é uma mensagem de saudação inicial
            greeting_messages = ["olá", "ola", "oi", "hello", "hi", "bom dia", "boa tarde", "boa noite"]
            is_greeting = message.lower().strip() in greeting_messages
            
            # Se é saudação e não tem dados ainda, iniciar fluxo
            if is_greeting and not session_data.get("lead_data"):
                flow = await get_conversation_flow()
                steps = flow.get("steps", [])
                first_step = next((s for s in steps if s["id"] == 1), None)
                
                if first_step:
                    session_data.update({
                        "current_step": 1,
                        "message_count": 1,
                        "lead_data": {},
                        "last_updated": datetime.now()
                    })
                    await save_user_session(session_id, session_data)
                    
                    return {
                        "response": first_step["question"],
                        "response_type": "structured_question",
                        "session_id": session_id,
                        "current_step": 1,
                        "flow_completed": False
                    }
            # Verificar se está coletando telefone
            if session_data.get("collecting_phone"):
                return await self._handle_phone_collection(message, session_id, session_data)

            # Verificar se fluxo foi completado
            if session_data.get("flow_completed"):
                # Se já completou, usar IA
                return await self._handle_ai_mode(message, session_id, session_data)

            # Processar fluxo estruturado
            return await self._handle_structured_flow(message, session_id, session_data, platform)

        except Exception as e:
            logger.error(f"❌ Erro no orchestrator | session={session_id}: {str(e)}")
            return {
                "response": "Desculpe, ocorreu um erro. Vamos recomeçar. Qual é o seu nome completo?",
                "response_type": "error_recovery",
                "session_id": session_id,
                "error": str(e)
            }

    async def _create_new_session(self, session_id: str, platform: str) -> Dict[str, Any]:
        """Cria nova sessão com dados iniciais."""
        session_data = {
            "session_id": session_id,
            "platform": platform,
            "current_step": 1,
            "flow_completed": False,
            "collecting_phone": False,
            "phone_submitted": False,
            "lead_data": {},
            "message_count": 0,
            "created_at": datetime.now(),
            "last_updated": datetime.now()
        }
        
        await save_user_session(session_id, session_data)
        logger.info(f"✅ Nova sessão criada | session={session_id} | platform={platform}")
        return session_data

    async def _handle_structured_flow(
        self,
        message: str,
        session_id: str,
        session_data: Dict[str, Any],
        platform: str
    ) -> Dict[str, Any]:
        """
        Gerencia o fluxo estruturado com lógica corrigida de avanço.
        """
        try:
            # Buscar fluxo de conversa
            flow = await get_conversation_flow()
            steps = flow.get("steps", [])
            
            current_step = session_data.get("current_step", 1)
            message_count = session_data.get("message_count", 0) + 1
            
            logger.info(f"📋 Fluxo estruturado | step={current_step} | total_steps={len(steps)} | msg_count={message_count}")

            # Se é a primeira mensagem (saudação inicial), mostrar primeira pergunta
            if message_count == 1 and message.lower().strip() in ["olá", "ola", "oi", "hello", "hi"]:
                first_step = next((s for s in steps if s["id"] == 1), None)
                if first_step:
                    session_data.update({
                        "current_step": 1,
                        "message_count": 1,
                        "last_updated": datetime.now()
                    })
                    await save_user_session(session_id, session_data)
                    
                    return {
                        "response": first_step["question"],
                        "response_type": "structured_question",
                        "session_id": session_id,
                        "current_step": 1,
                        "flow_completed": False
                    }

            # Processar resposta do usuário
            step_data = next((s for s in steps if s["id"] == current_step), None)
            if not step_data:
                logger.error(f"❌ Step {current_step} não encontrado no fluxo")
                return await self._handle_ai_mode(message, session_id, session_data)

            # Validar resposta
            if not self._validate_answer(message, current_step):
                logger.info(f"⚠️ Resposta inválida para step {current_step}, repetindo pergunta")
                return {
                    "response": step_data["question"],
                    "response_type": "validation_retry",
                    "session_id": session_id,
                    "current_step": current_step,
                    "flow_completed": False
                }

            # Salvar resposta
            field_name = f"step_{current_step}"
            session_data["lead_data"][field_name] = message.strip()
            
            # Normalizar área jurídica se for step 3
            if current_step == 3:
                session_data["lead_data"][field_name] = self._normalize_legal_area(message)

            logger.info(f"💾 Resposta salva | step={current_step} | answer='{message[:30]}...'")

            # Verificar se é o último step
            next_step = current_step + 1
            next_step_data = next((s for s in steps if s["id"] == next_step), None)

            if next_step_data:
                # Avançar para próximo step
                session_data.update({
                    "current_step": next_step,
                    "message_count": message_count,
                    "last_updated": datetime.now()
                })
                await save_user_session(session_id, session_data)

                # Personalizar pergunta com nome do usuário
                next_question = next_step_data["question"]
                user_name = session_data["lead_data"].get("step_1", "")
                if user_name and "{user_name}" in next_question:
                    next_question = next_question.replace("{user_name}", user_name.split()[0])

                logger.info(f"➡️ Avançando para step {next_step}")
                
                return {
                    "response": next_question,
                    "response_type": "structured_question",
                    "session_id": session_id,
                    "current_step": next_step,
                    "flow_completed": False
                }
            else:
                # Fluxo completado, iniciar coleta de telefone
                return await self._complete_structured_flow(session_id, session_data, message_count)

        except Exception as e:
            logger.error(f"❌ Erro no fluxo estruturado | session={session_id}: {str(e)}")
            return await self._handle_ai_mode(message, session_id, session_data)

    def _validate_answer(self, answer: str, step_id: int) -> bool:
        """Valida resposta baseada no step."""
        answer = answer.strip()
        
        if len(answer) < 2:
            return False
            
        if step_id == 1:  # Nome
            return len(answer.split()) >= 2
        elif step_id == 2:  # Contato
            return len(answer) >= 10
        elif step_id == 3:  # Área jurídica
            return len(answer) >= 3
        elif step_id == 4:  # Situação
            return len(answer) >= 10
        elif step_id == 5:  # Confirmação
            return len(answer) >= 1
            
        return True

    def _normalize_legal_area(self, area: str) -> str:
        """Normaliza área jurídica."""
        area_lower = area.lower().strip()
        
        area_map = {
            "penal": "Direito Penal",
            "criminal": "Direito Penal",
            "crime": "Direito Penal",
            "saude": "Saúde/Liminares",
            "saúde": "Saúde/Liminares",
            "liminar": "Saúde/Liminares",
            "liminares": "Saúde/Liminares",
            "medica": "Saúde/Liminares",
            "médica": "Saúde/Liminares"
        }
        
        return area_map.get(area_lower, area.title())

    async def _complete_structured_flow(
        self,
        session_id: str,
        session_data: Dict[str, Any],
        message_count: int
    ) -> Dict[str, Any]:
        """Completa o fluxo estruturado e inicia coleta de telefone."""
        try:
            # Salvar lead no Firebase
            lead_data = {
                "answers": [
                    {"id": 1, "answer": session_data["lead_data"].get("step_1", "")},
                    {"id": 2, "answer": session_data["lead_data"].get("step_2", "")},
                    {"id": 3, "answer": session_data["lead_data"].get("step_3", "")},
                    {"id": 4, "answer": session_data["lead_data"].get("step_4", "")},
                    {"id": 5, "answer": session_data["lead_data"].get("step_5", "")}
                ],
                "session_id": session_id,
                "platform": session_data.get("platform", "web"),
                "completed_at": datetime.now(),
                "status": "flow_completed"
            }
            
            lead_id = await save_lead_data(lead_data)
            logger.info(f"💾 Lead salvo | lead_id={lead_id}")

            # Atualizar sessão
            session_data.update({
                "flow_completed": True,
                "collecting_phone": True,
                "lead_id": lead_id,
                "message_count": message_count,
                "last_updated": datetime.now()
            })
            await save_user_session(session_id, session_data)

            user_name = session_data["lead_data"].get("step_1", "").split()[0]
            phone_message = f"Perfeito, {user_name}! Para finalizar, preciso do seu número de WhatsApp com DDD (ex: 11999999999):"

            return {
                "response": phone_message,
                "response_type": "phone_collection",
                "session_id": session_id,
                "flow_completed": True,
                "collecting_phone": True,
                "lead_id": lead_id
            }

        except Exception as e:
            logger.error(f"❌ Erro ao completar fluxo | session={session_id}: {str(e)}")
            return {
                "response": "Obrigado pelas informações! Como posso ajudá-lo mais?",
                "response_type": "flow_completion_error",
                "session_id": session_id,
                "flow_completed": True
            }

    async def _handle_phone_collection(
        self,
        message: str,
        session_id: str,
        session_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Coleta e processa número de telefone."""
        try:
            phone_clean = ''.join(filter(str.isdigit, message))
            
            # Validar telefone brasileiro
            if len(phone_clean) < 10 or len(phone_clean) > 13:
                return {
                    "response": "Número inválido. Digite apenas os números com DDD (ex: 11999999999):",
                    "response_type": "phone_validation_error",
                    "session_id": session_id,
                    "collecting_phone": True
                }

            # Formatar telefone
            if not phone_clean.startswith("55"):
                phone_clean = f"55{phone_clean}"

            # Atualizar sessão
            session_data.update({
                "collecting_phone": False,
                "phone_submitted": True,
                "phone_number": phone_clean,
                "last_updated": datetime.now()
            })
            await save_user_session(session_id, session_data)

            # Atualizar lead
            if session_data.get("lead_id"):
                await update_lead_data(session_data["lead_id"], {
                    "phone_number": phone_clean,
                    "status": "phone_collected"
                })

            # Enviar mensagem WhatsApp
            user_name = session_data["lead_data"].get("step_1", "Cliente").split()[0]
            area = session_data["lead_data"].get("step_3", "sua área")
            
            whatsapp_message = f"Olá {user_name}! 👋\n\nRecebemos suas informações sobre {area} e nossa equipe entrará em contato em breve.\n\nObrigado por escolher nossos serviços!"
            
            whatsapp_success = False
            try:
                whatsapp_target = f"{phone_clean}@s.whatsapp.net"
                whatsapp_success = await baileys_service.send_whatsapp_message(whatsapp_target, whatsapp_message)
                logger.info(f"📱 WhatsApp enviado | success={whatsapp_success} | phone={phone_clean}")
            except Exception as whatsapp_error:
                logger.error(f"❌ Erro WhatsApp | phone={phone_clean}: {str(whatsapp_error)}")

            # Notificar advogados
            try:
                await lawyer_notification_service.notify_lawyers_of_new_lead(
                    lead_name=user_name,
                    lead_phone=phone_clean,
                    category=area,
                    additional_info=session_data["lead_data"]
                )
                logger.info(f"👨‍💼 Advogados notificados | lead={user_name}")
            except Exception as notify_error:
                logger.error(f"❌ Erro notificação advogados: {str(notify_error)}")

            confirmation_message = f"✅ Número confirmado: {phone_clean}\n\nSuas informações foram registradas e nossa equipe entrará em contato em breve!"
            
            if whatsapp_success:
                confirmation_message += "\n\n📱 Mensagem de confirmação enviada para seu WhatsApp!"

            return {
                "response": confirmation_message,
                "response_type": "phone_collected",
                "session_id": session_id,
                "flow_completed": True,
                "phone_submitted": True,
                "phone_number": phone_clean,
                "whatsapp_sent": whatsapp_success
            }

        except Exception as e:
            logger.error(f"❌ Erro coleta telefone | session={session_id}: {str(e)}")
            return {
                "response": "Erro ao processar telefone. Vamos continuar! Como posso ajudá-lo?",
                "response_type": "phone_collection_error",
                "session_id": session_id,
                "flow_completed": True,
                "collecting_phone": False
            }

    async def _handle_ai_mode(
        self,
        message: str,
        session_id: str,
        session_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Processa mensagem via IA."""
        try:
            # Tentar Gemini primeiro
            if await self._is_gemini_available():
                try:
                    context = {
                        "platform": session_data.get("platform", "web"),
                        "name": session_data.get("lead_data", {}).get("step_1", ""),
                        "area_of_law": session_data.get("lead_data", {}).get("step_3", ""),
                        "situation": session_data.get("lead_data", {}).get("step_4", "")
                    }
                    
                    ai_response = await ai_orchestrator.generate_response(
                        message, session_id, context
                    )
                    
                    logger.info(f"🤖 Resposta IA gerada | session={session_id}")
                    
                    return {
                        "response": ai_response,
                        "response_type": "ai_intelligent",
                        "session_id": session_id,
                        "ai_mode": True,
                        "gemini_available": True
                    }
                    
                except Exception as ai_error:
                    logger.error(f"❌ Erro IA | session={session_id}: {str(ai_error)}")
                    if self._is_quota_error(str(ai_error)):
                        self._mark_gemini_unavailable()

            # Fallback para resposta padrão
            return {
                "response": "Obrigado por entrar em contato! Nossa equipe analisará sua mensagem e retornará em breve.",
                "response_type": "fallback_standard",
                "session_id": session_id,
                "ai_mode": True,
                "gemini_available": False
            }

        except Exception as e:
            logger.error(f"❌ Erro modo IA | session={session_id}: {str(e)}")
            return {
                "response": "Como posso ajudá-lo?",
                "response_type": "ai_error",
                "session_id": session_id,
                "ai_mode": True
            }

    async def _is_gemini_available(self) -> bool:
        """Verifica se Gemini está disponível."""
        if not self.gemini_unavailable_until:
            return True
        
        if datetime.now() > self.gemini_unavailable_until:
            self.gemini_unavailable_until = None
            return True
            
        return False

    def _mark_gemini_unavailable(self):
        """Marca Gemini como indisponível temporariamente."""
        self.gemini_unavailable_until = datetime.now() + self.gemini_check_interval
        logger.warning(f"🚫 Gemini marcado indisponível até {self.gemini_unavailable_until}")

    def _is_quota_error(self, error_message: str) -> bool:
        """Detecta erros de quota do Gemini."""
        error_lower = error_message.lower()
        quota_indicators = ["429", "quota", "rate limit", "resourceexhausted", "billing"]
        return any(indicator in error_lower for indicator in quota_indicators)

    async def handle_phone_number_submission(
        self,
        phone_number: str,
        session_id: str,
        user_name: str = "Cliente"
    ) -> Dict[str, Any]:
        """Processa submissão de telefone via web."""
        try:
            session_data = await get_user_session(session_id)
            if not session_data:
                return {"success": False, "error": "Sessão não encontrada"}

            phone_clean = ''.join(filter(str.isdigit, phone_number))
            if not phone_clean.startswith("55"):
                phone_clean = f"55{phone_clean}"

            # Atualizar sessão
            session_data.update({
                "phone_submitted": True,
                "phone_number": phone_clean,
                "last_updated": datetime.now()
            })
            await save_user_session(session_id, session_data)

            return {
                "success": True,
                "message": "Telefone registrado com sucesso!",
                "phone_number": phone_clean
            }

        except Exception as e:
            logger.error(f"❌ Erro submissão telefone | session={session_id}: {str(e)}")
            return {"success": False, "error": str(e)}

    async def handle_whatsapp_authorization(self, auth_data: Dict[str, Any]):
        """Processa autorização WhatsApp."""
        try:
            session_id = auth_data.get("session_id")
            phone_number = auth_data.get("phone_number")
            source = auth_data.get("source", "unknown")
            
            logger.info(f"🔐 Processando autorização WhatsApp | session={session_id} | source={source}")
            
            # Criar ou atualizar sessão WhatsApp
            session_data = {
                "session_id": session_id,
                "platform": "whatsapp",
                "phone_number": phone_number,
                "source": source,
                "authorized": True,
                "current_step": 1,
                "flow_completed": False,
                "collecting_phone": False,
                "phone_submitted": False,
                "lead_data": auth_data.get("user_data", {}),
                "message_count": 0,
                "created_at": datetime.now(),
                "last_updated": datetime.now()
            }
            
            await save_user_session(session_id, session_data)
            logger.info(f"✅ Sessão WhatsApp autorizada | session={session_id}")
            
        except Exception as e:
            logger.error(f"❌ Erro autorização WhatsApp: {str(e)}")

    async def get_session_context(self, session_id: str) -> Dict[str, Any]:
        """Obtém contexto da sessão."""
        try:
            session_data = await get_user_session(session_id)
            if not session_data:
                return {"exists": False}

            return {
                "exists": True,
                "session_id": session_id,
                "platform": session_data.get("platform", "unknown"),
                "current_step": session_data.get("current_step", 1),
                "flow_completed": session_data.get("flow_completed", False),
                "collecting_phone": session_data.get("collecting_phone", False),
                "phone_submitted": session_data.get("phone_submitted", False),
                "message_count": session_data.get("message_count", 0),
                "lead_data": session_data.get("lead_data", {}),
                "created_at": session_data.get("created_at"),
                "last_updated": session_data.get("last_updated")
            }

        except Exception as e:
            logger.error(f"❌ Erro ao buscar contexto | session={session_id}: {str(e)}")
            return {"exists": False, "error": str(e)}

    async def reset_session(self, session_id: str) -> Dict[str, Any]:
        """Reseta uma sessão."""
        try:
            session_data = {
                "session_id": session_id,
                "current_step": 1,
                "flow_completed": False,
                "collecting_phone": False,
                "phone_submitted": False,
                "lead_data": {},
                "message_count": 0,
                "reset_at": datetime.now(),
                "last_updated": datetime.now()
            }
            
            await save_user_session(session_id, session_data)
            logger.info(f"🔄 Sessão resetada | session={session_id}")
            
            return {"success": True, "message": "Sessão resetada com sucesso"}

        except Exception as e:
            logger.error(f"❌ Erro ao resetar sessão | session={session_id}: {str(e)}")
            return {"success": False, "error": str(e)}

    async def get_overall_service_status(self) -> Dict[str, Any]:
        """Obtém status geral dos serviços."""
        try:
            from app.services.firebase_service import get_firebase_service_status
            from app.services.ai_chain import get_ai_service_status
            
            firebase_status = await get_firebase_service_status()
            ai_status = await get_ai_service_status()
            
            overall_status = "active"
            if firebase_status.get("status") != "active":
                overall_status = "degraded"
            if ai_status.get("status") not in ["active", "degraded"]:
                overall_status = "degraded"

            return {
                "overall_status": overall_status,
                "firebase_status": firebase_status,
                "ai_status": ai_status,
                "gemini_available": await self._is_gemini_available(),
                "fallback_mode": not await self._is_gemini_available(),
                "features": {
                    "structured_flow": True,
                    "ai_responses": ai_status.get("status") == "active",
                    "phone_collection": True,
                    "whatsapp_integration": True,
                    "lawyer_notifications": True
                },
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"❌ Erro ao obter status geral: {str(e)}")
            return {
                "overall_status": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

# Instância global
intelligent_orchestrator = IntelligentHybridOrchestrator()
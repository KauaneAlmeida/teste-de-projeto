import logging
import json
import os
import re
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from app.services.firebase_service import (
    get_user_session,
    save_user_session,
    save_lead_data,
    get_conversation_flow,
    get_firebase_service_status
)
from app.services.ai_chain import ai_orchestrator
from app.services.baileys_service import baileys_service
from app.services.lawyer_notification_service import lawyer_notification_service

logger = logging.getLogger(__name__)


def ensure_utc(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class IntelligentHybridOrchestrator:
    def __init__(self):
        self.gemini_available = True
        self.gemini_timeout = 15.0
        self.law_firm_number = "+5511918368812"
        self.schema_flow_cache = None
        self.cache_timestamp = None

        # Lista de respostas inválidas comuns para evitar pulos
        self.invalid_responses = {
            'greetings': ['oi', 'olá', 'ola', 'hello', 'hi', 'hey', 'e ai', 'eai', 'opa'],
            'short_responses': ['ok', 'sim', 'não', 'nao', 'yes', 'no', 'k', 'kk', 'kkk'],
            'test_responses': ['teste', 'test', '123', 'abc', 'aaa', 'bbb', 'ccc', 'xxx'],
            'generic': ['p.o.', 'po', 'p.o', '.', '..', '...', 'a', 'aa', 'bb', 'cc']
        }

    def _format_brazilian_phone(self, phone_clean: str) -> str:
        """Format Brazilian phone number correctly for WhatsApp."""
        try:
            if not phone_clean:
                return ""
            phone_clean = ''.join(filter(str.isdigit, str(phone_clean)))

            # Remove existing country code
            if phone_clean.startswith("55"):
                phone_clean = phone_clean[2:]

            # Normalize lengths
            if len(phone_clean) == 8:
                return f"55{phone_clean}"
            if len(phone_clean) == 9:
                return f"55{phone_clean}"
            if len(phone_clean) == 10:
                ddd = phone_clean[:2]
                number = phone_clean[2:]
                if len(number) == 8 and number[0] in ['6', '7', '8', '9']:
                    number = f"9{number}"
                return f"55{ddd}{number}"
            if len(phone_clean) == 11:
                ddd = phone_clean[:2]
                number = phone_clean[2:]
                return f"55{ddd}{number}"
            return f"55{phone_clean}"
        except Exception as e:
            logger.error(f"❌ Error formatting phone number {phone_clean}: {str(e)}")
            return f"55{phone_clean if phone_clean else ''}"

    def _is_invalid_response(self, response: str, context: str = "general") -> bool:
        if not response or not response.strip():
            return True
            
        response_lower = response.lower().strip()
        
        # Respostas muito curtas (menos de 2 caracteres)
        if len(response_lower) < 2:
            return True
            
        # Apenas números muito pequenos
        if response_lower.isdigit() and len(response_lower) < 4:
            return True
            
        # Apenas caracteres repetidos
        if len(set(response_lower.replace(' ', ''))) <= 2 and len(response_lower) < 4:
            return True
            
        # Verificar listas de respostas inválidas
        all_invalid = []
        for category in self.invalid_responses.values():
            all_invalid.extend(category)
            
        return response_lower in all_invalid

    async def get_gemini_health_status(self) -> Dict[str, Any]:
        try:
            test_response = await asyncio.wait_for(
                ai_orchestrator.generate_response("test", session_id="__health_check__"),
                timeout=5.0
            )
            ai_orchestrator.clear_session_memory("__health_check__")
            if test_response and isinstance(test_response, str) and test_response.strip():
                self.gemini_available = True
                return {"service": "gemini_ai", "status": "active", "available": True, "message": "Gemini AI is operational"}
            else:
                self.gemini_available = False
                return {"service": "gemini_ai", "status": "inactive", "available": False, "message": "Gemini AI returned invalid response"}
        except asyncio.TimeoutError:
            self.gemini_available = False
            return {"service": "gemini_ai", "status": "inactive", "available": False, "message": "Gemini AI timeout - likely quota exceeded"}
        except Exception as e:
            self.gemini_available = False
            error_str = str(e).lower()
            if self._is_quota_error(error_str):
                return {"service": "gemini_ai", "status": "quota_exceeded", "available": False, "message": f"Gemini API quota exceeded: {str(e)}"}
            else:
                return {"service": "gemini_ai", "status": "error", "available": False, "message": f"Gemini AI error: {str(e)}"}

    async def get_overall_service_status(self) -> Dict[str, Any]:
        try:
            firebase_status = await get_firebase_service_status()
            ai_status = await self.get_gemini_health_status()
            firebase_healthy = firebase_status.get("status") == "active"
            ai_healthy = ai_status.get("status") == "active"
            if firebase_healthy and ai_healthy:
                overall_status = "active"
            elif firebase_healthy:
                overall_status = "degraded"
            else:
                overall_status = "error"
            return {
                "overall_status": overall_status,
                "firebase_status": firebase_status,
                "ai_status": ai_status,
                "features": {
                    "conversation_flow": firebase_healthy,
                    "ai_responses": ai_healthy,
                    "fallback_mode": firebase_healthy and not ai_healthy,
                    "whatsapp_integration": True,
                    "lead_collection": firebase_healthy
                },
                "gemini_available": self.gemini_available,
                "fallback_mode": not self.gemini_available
            }
        except Exception as e:
            logger.error(f"❌ Error getting overall service status: {str(e)}")
            return {
                "overall_status": "error",
                "firebase_status": {"status": "error", "error": str(e)},
                "ai_status": {"status": "error", "error": str(e)},
                "features": {"conversation_flow": False, "ai_responses": False, "fallback_mode": False, "whatsapp_integration": False, "lead_collection": False},
                "gemini_available": False,
                "fallback_mode": True,
                "error": str(e)
            }

    async def _get_or_create_session(self, session_id: str, platform: str, phone_number: Optional[str] = None) -> Dict[str, Any]:
        logger.info(f"🔍 DEBUG: Getting/creating session {session_id} for platform {platform}")
        
        session_data = await get_user_session(session_id)
        logger.info(f"🔍 DEBUG: Existing session data: {session_data is not None}")
        
        if not session_data:
            session_data = {
                "session_id": session_id,
                "platform": platform,
                "created_at": ensure_utc(datetime.now(timezone.utc)),
                "lead_data": {},
                "message_count": 0,
                "fallback_step": None,  # Começará em None para ser inicializado
                "phone_submitted": False,
                "gemini_available": True,
                "last_gemini_check": None,
                "fallback_completed": False,
                "lead_qualified": False,
                "validation_attempts": {},
                "session_started": False,
                "flow_initialized": False  # NOVO CAMPO PARA DEBUG
            }
            logger.info(f"🆕 DEBUG: Created new session {session_id} for platform {platform}")
        else:
            logger.info(f"📊 DEBUG: Session state - Step: {session_data.get('fallback_step')}, Started: {session_data.get('session_started')}, Flow initialized: {session_data.get('flow_initialized')}")
            
        if phone_number:
            session_data["phone_number"] = phone_number
        return session_data

    def _is_quota_error(self, error_message: str) -> bool:
        quota_indicators = ["429", "quota", "rate limit", "exceeded", "resourceexhausted", "billing", "plan", "free tier", "requests per day"]
        return any(indicator in str(error_message).lower() for indicator in quota_indicators)

    def _is_phone_number(self, message: str) -> bool:
        clean_message = ''.join(filter(str.isdigit, (message or "")))
        return 10 <= len(clean_message) <= 13

    async def _get_schema_flow(self) -> Dict[str, Any]:
        """CORREÇÃO: Usar flow simplificado e hardcoded para evitar problemas de Firebase"""
        logger.info("🔍 DEBUG: Loading schema flow")
        
        try:
            # Usar flow hardcoded para garantir funcionamento
            hardcoded_flow = {
                "enabled": True,
                "sequential": True,
                "steps": [
                    {
                        "id": 1, 
                        "field": "identification", 
                        "question": "Olá! Seja bem-vindo ao m.lima. Estou aqui para entender seu caso e agilizar o contato com um de nossos advogados especializados.\n\nPara começar, qual é o seu nome completo?", 
                        "validation": {"min_length": 2, "min_words": 1, "required": True, "type": "name", "strict": True}, 
                        "error_message": "Por favor, informe seu nome completo (nome e sobrenome). Exemplo: João Silva"
                    },
                    {
                        "id": 2, 
                        "field": "contact_info", 
                        "question": "Prazer em conhecê-lo, {user_name}! Agora preciso de algumas informações de contato:\n\n📱 Qual o melhor telefone/WhatsApp para contato?\n📧 Você poderia informar seu e-mail também?", 
                        "validation": {"min_length": 10, "required": True, "type": "contact_combined", "strict": True}, 
                        "error_message": "Por favor, informe seu telefone (com DDD) e e-mail. Exemplo: (11) 99999-9999 - joao@email.com"
                    },
                    {
                        "id": 3, 
                        "field": "area_qualification", 
                        "question": "Perfeito, {user_name}! Com qual área do direito você precisa de ajuda?\n\n• Penal\n• Saúde (ações e liminares médicas)", 
                        "validation": {"min_length": 3, "required": True, "type": "area", "strict": True}, 
                        "error_message": "Por favor, escolha uma das áreas disponíveis: Penal ou Saúde (liminares médicas)."
                    },
                    {
                        "id": 4, 
                        "field": "case_details", 
                        "question": "Entendi, {user_name}. Me diga de forma breve sobre sua situação em {area}:\n\n• O caso já está em andamento na justiça ou é uma situação inicial?\n• Existe algum prazo ou audiência marcada?\n• Em qual cidade ocorreu/está ocorrendo?", 
                        "validation": {"min_length": 20, "min_words": 5, "required": True, "type": "case_description", "strict": True}, 
                        "error_message": "Por favor, me conte mais detalhes sobre sua situação. Preciso de pelo menos 20 caracteres para entender seu caso adequadamente."
                    },
                    {
                        "id": 5, 
                        "field": "lead_warming", 
                        "question": "Obrigado por compartilhar, {user_name}. Casos como o seu em {area} exigem atenção imediata para evitar complicações.\n\nNossos advogados já atuaram em dezenas de casos semelhantes com ótimos resultados. Vou registrar os principais pontos para que o advogado responsável já entenda sua situação e agilize a solução.\n\nEm instantes você será direcionado para um de nossos especialistas. Está tudo certo?", 
                        "validation": {"min_length": 1, "required": True, "type": "confirmation", "strict": False}, 
                        "error_message": "Por favor, confirme se posso prosseguir com o direcionamento. Digite 'sim' ou 'não'."
                    }
                ],
                "completion_message": "Perfeito, {user_name}! Um de nossos advogados especialistas em {area} já vai assumir seu atendimento em instantes.\n\nEnquanto isso, fique tranquilo - você está em boas mãos! 🤝\n\nSuas informações foram registradas e o advogado já terá todo o contexto do seu caso."
            }
            
            logger.info(f"✅ DEBUG: Schema flow loaded with {len(hardcoded_flow.get('steps', []))} steps")
            return hardcoded_flow
            
        except Exception as e:
            logger.error(f"❌ DEBUG: Error loading schema flow: {str(e)}")
            # Return minimal flow if everything fails
            return {
                "enabled": True, 
                "sequential": True, 
                "steps": [
                    {"id": 1, "field": "identification", "question": "Qual é o seu nome completo?", "validation": {"required": True}}
                ], 
                "completion_message": "Obrigado! Nossa equipe entrará em contato."
            }

    async def _get_fallback_response(self, session_data: Dict[str, Any], message: str) -> str:
        """FALLBACK RESPONSE COM LOGS DETALHADOS PARA DEBUG"""
        try:
            session_id = session_data["session_id"]
            platform = session_data.get("platform", "web")

            logger.info(f"🔍 DEBUG: ===== FALLBACK RESPONSE START =====")
            logger.info(f"🔍 DEBUG: Session: {session_id}, Platform: {platform}")
            logger.info(f"🔍 DEBUG: Message: '{message}'")
            logger.info(f"🔍 DEBUG: Current step: {session_data.get('fallback_step')}")
            logger.info(f"🔍 DEBUG: Session started: {session_data.get('session_started')}")
            logger.info(f"🔍 DEBUG: Flow initialized: {session_data.get('flow_initialized')}")
            logger.info(f"🔍 DEBUG: Lead data: {session_data.get('lead_data')}")

            flow = await self._get_schema_flow()
            steps = flow.get("steps", []) or []
            steps = sorted(steps, key=lambda x: x.get("id", 0))

            if not steps:
                logger.error("❌ DEBUG: No steps found in schema flow")
                return "Olá! Seja bem-vindo ao m.lima. Vamos começar me diz seu nome?"

            logger.info(f"✅ DEBUG: Flow has {len(steps)} steps")

            # Inicializar validation_attempts se não existir
            if "validation_attempts" not in session_data:
                session_data["validation_attempts"] = {}
                logger.info("🔧 DEBUG: Initialized validation_attempts")

            # INICIALIZAÇÃO DO FLUXO
            if not session_data.get("flow_initialized", False):
                logger.info("🆕 DEBUG: INITIALIZING FLOW FOR THE FIRST TIME")
                session_data["fallback_step"] = 1
                session_data["lead_data"] = {}
                session_data["fallback_completed"] = False
                session_data["lead_qualified"] = False
                session_data["validation_attempts"] = {1: 0}
                session_data["session_started"] = True
                session_data["flow_initialized"] = True
                
                await save_user_session(session_id, session_data)
                logger.info("✅ DEBUG: Session initialized and saved")
                
                first_step = steps[0] if steps else None
                if first_step:
                    response = self._interpolate_message(first_step["question"], {})
                    logger.info(f"📤 DEBUG: Sending initial question: '{response[:100]}...'")
                    return response
                else:
                    logger.error("❌ DEBUG: No first step found")
                    return "Olá! Seja bem-vindo ao m.lima. Conte me seu nome completo?"

            # VERIFICAR SE FLUXO JÁ COMPLETADO
            if session_data.get("fallback_completed", False):
                user_name = session_data.get("lead_data", {}).get("identification", "")
                logger.info(f"✅ DEBUG: Flow already completed for user: {user_name}")
                return f"Obrigado {user_name}! Nossa equipe já foi notificada e entrará em contato em breve. 🤝"

            # OBTER STEP ATUAL
            current_step_id = session_data.get("fallback_step", 1)
            lead_data = session_data.get("lead_data", {})
            validation_attempts = session_data.get("validation_attempts", {})

            logger.info(f"📊 DEBUG: Processing step {current_step_id}")
            logger.info(f"📊 DEBUG: Lead data so far: {lead_data}")
            logger.info(f"📊 DEBUG: Validation attempts: {validation_attempts}")

            # Garantir que existe contador para step atual
            if current_step_id not in validation_attempts:
                validation_attempts[current_step_id] = 0
                logger.info(f"🔧 DEBUG: Initialized validation counter for step {current_step_id}")

            current_step = next((s for s in steps if s["id"] == current_step_id), None)
            if not current_step:
                logger.error(f"❌ DEBUG: Step {current_step_id} not found, resetting to step 1")
                session_data["fallback_step"] = 1
                session_data["validation_attempts"] = {1: 0}
                await save_user_session(session_id, session_data)
                first_step = steps[0] if steps else None
                if first_step:
                    return self._interpolate_message(first_step.get("question", ""), {})
                return "Olá! Seja bem-vindo ao m.lima. Para início, qual é o seu nome ao todo?"

            logger.info(f"✅ DEBUG: Found current step: {current_step.get('field', 'unknown_field')}")

            # TRATAR SAUDAÇÕES APENAS NO PRIMEIRO STEP
            if current_step_id == 1:
                message_lower = (message or "").lower().strip()
                if message_lower in ['oi', 'olá', 'hello', 'hi', 'ola', 'hey', 'e ai', 'eai']:
                    logger.info("👋 DEBUG: Greeting detected in step 1, re-sending question")
                    return self._interpolate_message(current_step["question"], lead_data)

            # VERIFICAR SE HÁ MENSAGEM VÁLIDA
            if not message or not message.strip():
                logger.info("📝 DEBUG: Empty message, re-sending current question")
                return self._interpolate_message(current_step.get("question", ""), lead_data)

            # PROCESSAR RESPOSTA DO USUÁRIO
            logger.info(f"⚙️ DEBUG: Processing user answer: '{message}'")
            
            validation_attempts[current_step_id] += 1
            session_data["validation_attempts"] = validation_attempts
            
            max_attempts = 3
            is_flexible = validation_attempts[current_step_id] > max_attempts

            logger.info(f"📊 DEBUG: Attempt {validation_attempts[current_step_id]}/{max_attempts}, Flexible mode: {is_flexible}")

            # VALIDAÇÃO
            normalized_answer = self._validate_and_normalize_answer_schema(message, current_step)
            should_advance = self._should_advance_step_schema(normalized_answer, current_step, is_flexible)

            logger.info(f"✅ DEBUG: Normalized answer: '{normalized_answer}'")
            logger.info(f"✅ DEBUG: Should advance: {should_advance}")

            if not should_advance:
                logger.info(f"❌ DEBUG: Validation failed for step {current_step_id}")
                
                # Construir mensagem de erro específica
                if validation_attempts[current_step_id] >= max_attempts:
                    if current_step_id == 1:
                        validation_msg = "Preciso do seu nome completo para continuar. Por favor, digite seu nome e sobrenome (exemplo: João Silva):"
                    elif current_step_id == 2:
                        validation_msg = "Preciso de seu telefone e/ou e-mail. Por favor, digite ao menos um contato válido:"
                    elif current_step_id == 3:
                        validation_msg = "Por favor, escolha apenas: 'Penal' ou 'Saúde'"
                    elif current_step_id == 4:
                        validation_msg = "Preciso de mais detalhes sobre sua situação jurídica. Conte-me pelo menos uma frase sobre seu caso:"
                    else:
                        validation_msg = "Por favor, confirme digitando 'sim' ou 'não':"
                else:
                    validation_msg = current_step.get("error_message", "Por favor, forneça uma resposta válida.")

                await save_user_session(session_id, session_data)
                question = self._interpolate_message(current_step["question"], lead_data)
                response = f"{validation_msg}\n\n{question}"
                logger.info(f"📤 DEBUG: Sending validation error: '{response[:100]}...'")
                return response

            # SUCESSO - SALVAR RESPOSTA E AVANÇAR
            logger.info(f"✅ DEBUG: Step {current_step_id} completed successfully")
            
            # Reset contador para step atual
            validation_attempts[current_step_id] = 0
            
            # Salvar resposta
            field_name = current_step.get("field", f"step_{current_step_id}")
            lead_data[field_name] = normalized_answer
            session_data["lead_data"] = lead_data

            logger.info(f"💾 DEBUG: Saved answer for field '{field_name}': '{normalized_answer}'")

            # EXTRAIR INFORMAÇÕES DE CONTATO SE STEP 2
            if current_step.get("field") == "contact_info":
                phone, email = self._extract_contact_info(normalized_answer)
                if phone:
                    session_data["lead_data"]["phone"] = phone
                    logger.info(f"📱 DEBUG: Extracted phone: {phone}")
                if email:
                    session_data["lead_data"]["email"] = email
                    logger.info(f"📧 DEBUG: Extracted email: {email}")

            # AVANÇAR PARA PRÓXIMO STEP
            next_step_id = current_step_id + 1
            next_step = next((s for s in steps if s["id"] == next_step_id), None)
            
            if next_step:
                logger.info(f"➡️ DEBUG: Advancing from step {current_step_id} to {next_step_id}")
                session_data["fallback_step"] = next_step_id
                validation_attempts[next_step_id] = 0
                session_data["validation_attempts"] = validation_attempts
                await save_user_session(session_id, session_data)
                
                response = self._interpolate_message(next_step.get("question", ""), lead_data)
                logger.info(f"📤 DEBUG: Sending next step question: '{response[:100]}...'")
                return response
            else:
                # FINALIZAR FLUXO
                logger.info("🏁 DEBUG: Flow completed, starting finalization")
                session_data["fallback_completed"] = True
                session_data["lead_qualified"] = True
                await save_user_session(session_id, session_data)
                
                return await self._handle_lead_finalization(session_id, session_data)

        except Exception as e:
            logger.error(f"❌ DEBUG: Exception in fallback response: {str(e)}")
            import traceback
            logger.error(f"❌ DEBUG: Traceback: {traceback.format_exc()}")
            return "Olá! Seja bem-vindo ao m.lima. Me conte como é seu nome inteiro?"

    def _interpolate_message(self, message: str, lead_data: Dict[str, Any]) -> str:
        try:
            if not message:
                return "Como posso ajudá-lo?"
            interpolation_data = {
                "user_name": lead_data.get("identification", ""),
                "area": lead_data.get("area_qualification", ""),
                "contact_info": lead_data.get("contact_info", ""),
                "case_details": lead_data.get("case_details", ""),
                "phone": lead_data.get("phone", ""),
                "case_summary": (lead_data.get("case_details", "")[:100] + "...") if lead_data.get("case_details", "") and len(lead_data.get("case_details", "")) > 100 else lead_data.get("case_details", "")
            }
            for key, value in interpolation_data.items():
                if value and f"{{{key}}}" in message:
                    message = message.replace(f"{{{key}}}", value)
            return message
        except Exception as e:
            logger.error(f"❌ Error interpolating message: {str(e)}")
            return message

    def _extract_contact_info(self, contact_text: str) -> tuple:
        phone_match = re.search(r'(\d{10,11})', contact_text or "")
        email_match = re.search(r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})', contact_text or "")
        phone = phone_match.group(1) if phone_match else ""
        email = email_match.group(1) if email_match else ""
        return phone, email

    def _validate_and_normalize_answer_schema(self, answer: str, step_config: Dict[str, Any]) -> str:
        """Normalize and lightly sanitize input according to step schema."""
        answer = (answer or "").strip()
        step_id = step_config.get("id", 0)
        validation = step_config.get("validation", {}) or {}

        logger.info(f"🔍 DEBUG: Validating answer for step {step_id}: '{answer}'")

        # normalization map (explicit keys -> normalized)
        normalize_map = validation.get("normalize_map", {}) or {}
        if normalize_map:
            answer_lower = answer.lower()
            for keyword, normalized in normalize_map.items():
                if keyword.lower() in answer_lower:
                    logger.info(f"✅ DEBUG: Normalized '{answer}' to '{normalized}' via map")
                    return normalized

        field_type = validation.get("type", "") or ""

        # name
        if field_type == "name" or step_id == 1:
            if self._is_invalid_response(answer, "name"):
                return answer
            words = [w for w in answer.split() if w.strip()]
            if len(words) >= 2:
                result = " ".join(word.capitalize() for word in words)
                logger.info(f"✅ DEBUG: Normalized name: '{result}'")
                return result
            result = answer.capitalize()
            logger.info(f"✅ DEBUG: Capitalized name: '{result}'")
            return result

        # contact combined
        if field_type == "contact_combined" or step_id == 2:
            logger.info(f"✅ DEBUG: Contact info kept as-is: '{answer}'")
            return answer

        # area
        if field_type == "area" or step_id == 3:
            answer_lower = answer.lower()
            area_mapping = {
                ("penal", "criminal", "crime", "direito penal"): "Direito Penal",
                ("saude", "saúde", "liminar", "saude liminar", "saúde liminar", "medica", "médica", "health", "injunction"): "Saúde/Liminares"
            }
            for keywords, normalized in area_mapping.items():
                if any(k in answer_lower for k in keywords):
                    logger.info(f"✅ DEBUG: Mapped area '{answer}' to '{normalized}'")
                    return normalized
            result = answer.title()
            logger.info(f"✅ DEBUG: Titlecased area: '{result}'")
            return result

        # case description
        if field_type == "case_description" or step_id == 4:
            logger.info(f"✅ DEBUG: Case details kept as-is: '{answer}'")
            return answer

        # confirmation
        if field_type == "confirmation" or step_id == 5:
            answer_lower = answer.lower()
            if any(conf in answer_lower for conf in ['sim', 'ok', 'pode', 'claro', 'vamos', 'confirmo', 'confirmado', 's', 'yes']):
                logger.info("✅ DEBUG: Confirmation detected")
                return "Confirmado"
            logger.info(f"✅ DEBUG: Confirmation kept as-is: '{answer}'")
            return answer

        # phone explicit
        if field_type == "phone":
            result = ''.join(filter(str.isdigit, answer))
            logger.info(f"✅ DEBUG: Phone normalized: '{result}'")
            return result

        logger.info(f"✅ DEBUG: Answer kept as-is: '{answer}'")
        return answer

    def _should_advance_step_schema(self, answer: str, step_config: Dict[str, Any], is_flexible: bool = False) -> bool:
        """VALIDAÇÃO COM LOGS DETALHADOS PARA DEBUG"""
        answer = (answer or "").strip()
        validation = step_config.get("validation", {}) or {}
        min_length = validation.get("min_length", 1)
        min_words = validation.get("min_words", 1)
        required = validation.get("required", True)
        step_id = step_config.get("id", 0)

        logger.info(f"🔍 DEBUG: ===== VALIDATION FOR STEP {step_id} =====")
        logger.info(f"🔍 DEBUG: Answer: '{answer}' (length: {len(answer)})")
        logger.info(f"🔍 DEBUG: Required: {required}, Min length: {min_length}, Min words: {min_words}")
        logger.info(f"🔍 DEBUG: Is flexible: {is_flexible}")

        if required and not answer:
            logger.info(f"❌ DEBUG: Step {step_id}: Required field is empty")
            return False

        # VALIDAÇÃO ESPECÍFICA POR STEP COM LOGS DETALHADOS
        if step_id == 1:  # Nome
            logger.info(f"🔍 DEBUG: Step 1 (Name) validation")
            
            if self._is_invalid_response(answer, "name"):
                logger.info(f"❌ DEBUG: Step 1: Invalid response detected: '{answer}'")
                return False
                
            if answer.isdigit():
                logger.info(f"❌ DEBUG: Step 1: Only numbers not accepted: '{answer}'")
                return False
                
            words = [w for w in answer.split() if w.strip() and len(w) >= 2]
            logger.info(f"🔍 DEBUG: Step 1: Valid words found: {words} (count: {len(words)})")
            
            if is_flexible:
                result = len(words) >= 1 and len(answer) >= 2
                logger.info(f"✅ DEBUG: Step 1 (flexible): {result} - Words: {len(words)}, Length: {len(answer)}")
                return result
            else:
                result = len(words) >= 1 and len(answer) >= 2
                logger.info(f"✅ DEBUG: Step 1 (strict): {result} - Valid words: {words}")
                return result

        if step_id == 2:  # Contato
            logger.info(f"🔍 DEBUG: Step 2 (Contact) validation")
            
            answer_lower = answer.lower()
            has_phone = bool(re.search(r'\d{10,11}', answer))
            has_email = bool(re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', answer))
            has_contact_keywords = any(word in answer_lower for word in ['telefone', 'celular', 'whatsapp', 'email', 'gmail', 'hotmail'])
            
            logger.info(f"🔍 DEBUG: Step 2: Phone found: {has_phone}, Email found: {has_email}")
            logger.info(f"🔍 DEBUG: Step 2: Contact keywords found: {has_contact_keywords}")
            
            if is_flexible:
                result = has_phone or has_email or has_contact_keywords or len(answer) >= 8
                logger.info(f"✅ DEBUG: Step 2 (flexible): {result} - Phone: {has_phone}, Email: {has_email}, Keywords: {has_contact_keywords}")
                return result
            else:
                result = (has_phone or has_email) and len(answer) >= min_length
                logger.info(f"✅ DEBUG: Step 2 (strict): {result} - Phone: {has_phone}, Email: {has_email}")
                return result

        if step_id == 3:  # Área
            logger.info(f"🔍 DEBUG: Step 3 (Area) validation")
            
            answer_lower = answer.lower()
            valid_areas = ["penal", "criminal", "crime", "saude", "saúde", "liminar", "medica", "médica"]
            has_valid_area = any(area in answer_lower for area in valid_areas)
            
            logger.info(f"🔍 DEBUG: Step 3: Answer lower: '{answer_lower}'")
            logger.info(f"🔍 DEBUG: Step 3: Valid area detected: {has_valid_area}")
            
            if is_flexible:
                result = has_valid_area or len(answer) >= 4
                logger.info(f"✅ DEBUG: Step 3 (flexible): {result} - Valid area: {has_valid_area}")
                return result
            else:
                result = has_valid_area
                logger.info(f"✅ DEBUG: Step 3 (strict): {result} - Answer: '{answer_lower}', Valid: {has_valid_area}")
                return result

        if step_id == 4:  # Detalhes do caso
            logger.info(f"🔍 DEBUG: Step 4 (Case details) validation")
            
            words = [w for w in answer.split() if w.strip()]
            logger.info(f"🔍 DEBUG: Step 4: Words count: {len(words)}, Length: {len(answer)}")
            
            if is_flexible:
                result = len(answer) >= 15 and len(words) >= 4
                logger.info(f"✅ DEBUG: Step 4 (flexible): {result} - Length: {len(answer)}, Words: {len(words)}")
                return result
            else:
                result = len(answer) >= min_length and len(words) >= min_words
                logger.info(f"✅ DEBUG: Step 4 (strict): {result} - Length: {len(answer)}/{min_length}, Words: {len(words)}/{min_words}")
                return result

        if step_id == 5:  # Confirmação
            logger.info(f"🔍 DEBUG: Step 5 (Confirmation) validation")
            
            answer_lower = answer.lower()
            confirmations = ['sim', 'ok', 'pode', 'claro', 'vamos', 'confirmo', 'certo', 'yes', 's']
            has_confirmation = any(conf in answer_lower for conf in confirmations)
            
            logger.info(f"🔍 DEBUG: Step 5: Answer lower: '{answer_lower}'")
            logger.info(f"🔍 DEBUG: Step 5: Confirmation detected: {has_confirmation}")
            
            result = has_confirmation or len(answer) >= 2
            logger.info(f"✅ DEBUG: Step 5: {result} - Answer: '{answer_lower}'")
            return result

        # Default validation
        logger.info(f"🔍 DEBUG: Using default validation for step {step_id}")
        result = len(answer) >= (min_length if not is_flexible else 2)
        logger.info(f"✅ DEBUG: Step {step_id} (default): {result} - Length: {len(answer)}")
        return result

    async def _handle_lead_finalization(self, session_id: str, session_data: Dict[str, Any]) -> str:
        """FINALIZAÇÃO COM LOGS DETALHADOS"""
        try:
            logger.info(f"🏁 DEBUG: ===== LEAD FINALIZATION START =====")
            logger.info(f"🏁 DEBUG: Session ID: {session_id}")
            
            lead_data = session_data.get("lead_data", {}) or {}
            logger.info(f"🏁 DEBUG: Lead data: {lead_data}")

            # Extrair telefone
            phone_clean = lead_data.get("phone", "")
            if not phone_clean:
                contact_info = lead_data.get("contact_info", "")
                phone_match = re.search(r'(\d{10,11})', contact_info or "")
                phone_clean = phone_match.group(1) if phone_match else ""
                logger.info(f"📱 DEBUG: Extracted phone from contact_info: {phone_clean}")
            else:
                logger.info(f"📱 DEBUG: Phone already available: {phone_clean}")
                
            # VERIFICAR TELEFONE
            if not phone_clean or len(phone_clean) < 10:
                logger.warning(f"❌ DEBUG: Invalid phone number: '{phone_clean}'")
                return "Para finalizar, preciso do seu número de WhatsApp com DDD (exemplo: 11999999999):"

            # Formatar telefone
            phone_formatted = self._format_brazilian_phone(phone_clean)
            whatsapp_number = f"{phone_formatted}@s.whatsapp.net"
            
            logger.info(f"📱 DEBUG: Phone formatted: {phone_formatted}")
            logger.info(f"📱 DEBUG: WhatsApp number: {whatsapp_number}")

            # Atualizar dados da sessão
            session_data.update({
                "phone_number": phone_clean,
                "phone_formatted": phone_formatted,
                "phone_submitted": True,
                "lead_qualified": True,
                "qualification_completed_at": ensure_utc(datetime.now(timezone.utc)),
                "last_updated": ensure_utc(datetime.now(timezone.utc))
            })
            
            session_data["lead_data"]["phone"] = phone_clean
            await save_user_session(session_id, session_data)
            logger.info("💾 DEBUG: Session data updated")

            # Preparar dados para salvamento
            answers = []
            field_mapping = {
                "identification": 1,
                "contact_info": 2, 
                "area_qualification": 3,
                "case_details": 4,
                "lead_warming": 5
            }
            
            for field, step_id in field_mapping.items():
                answer = lead_data.get(field, "")
                if answer:
                    answers.append({"id": step_id, "answer": answer})
                    logger.info(f"📝 DEBUG: Added answer for step {step_id}: '{answer[:50]}...'")
            
            if phone_clean:
                answers.append({"id": 99, "field": "phone_extracted", "answer": phone_clean})
                logger.info(f"📱 DEBUG: Added extracted phone: {phone_clean}")

            # Salvar lead data
            try:
                lead_id = await save_lead_data({"answers": answers})
                logger.info(f"💾 DEBUG: Lead saved with ID: {lead_id}")
                
                # Preparar dados para notificação
                user_name = lead_data.get("identification", "Cliente")
                area = lead_data.get("area_qualification", "não informada")
                case_details = lead_data.get("case_details", "não detalhada")
                contact_info = lead_data.get("contact_info", "não informado")
                email = lead_data.get("email", "não informado")

                logger.info(f"👤 DEBUG: User name: {user_name}")
                logger.info(f"⚖️ DEBUG: Area: {area}")
                logger.info(f"📝 DEBUG: Case details: {case_details[:100]}...")

                # Notificar advogados
                try:
                    logger.info("📬 DEBUG: Sending lawyer notifications")
                    notification_result = await lawyer_notification_service.notify_lawyers_of_new_lead(
                        lead_name=user_name,
                        lead_phone=phone_clean,
                        category=area,
                        additional_info={
                            "case_details": case_details,
                            "contact_info": contact_info,
                            "email": email,
                            "urgency": "high",
                            "lead_temperature": "hot",
                            "flow_type": "fluxo_qualificacao_completo_debug",
                            "platform": session_data.get("platform", "web")
                        }
                    )
                    
                    if notification_result.get("success"):
                        notifications_sent = notification_result.get("notifications_sent", 0)
                        total_lawyers = notification_result.get("total_lawyers", 0)
                        logger.info(f"✅ DEBUG: Lawyers notified: {notifications_sent}/{total_lawyers}")
                    else:
                        logger.error(f"❌ DEBUG: Failed to notify lawyers: {notification_result.get('error', 'Unknown error')}")
                        
                except Exception as notification_error:
                    logger.error(f"❌ DEBUG: Error notifying lawyers: {str(notification_error)}")
                    
            except Exception as save_error:
                logger.error(f"❌ DEBUG: Error saving lead: {str(save_error)}")

            # Preparar resumo do caso
            case_summary = case_details[:100]
            if len(case_details) > 100:
                case_summary += "..."

            # Mensagem final do WhatsApp
            final_whatsapp_message = f"""Olá {user_name}! 👋

Recebemos sua solicitação de atendimento jurídico através do nosso sistema e nossa equipe especializada em {area} já foi notificada!

Um advogado experiente do m.lima entrará em contato diretamente com você no WhatsApp em breve. 🤝

📄 **Resumo do seu caso:**

👤 Nome: {user_name}
⚖️ Área: {area}
📝 Situação: {case_summary}

✅ Você está em excelentes mãos! Nossa equipe tem vasta experiência em casos similares.

Aguarde nosso contato! 💼"""

            # Enviar WhatsApp
            whatsapp_success = False
            try:
                logger.info(f"📤 DEBUG: Sending WhatsApp to: {whatsapp_number}")
                await baileys_service.send_whatsapp_message(whatsapp_number, final_whatsapp_message)
                logger.info(f"📤 DEBUG: WhatsApp sent successfully to {phone_formatted}")
                whatsapp_success = True
                
            except Exception as whatsapp_error:
                logger.error(f"❌ DEBUG: Error sending WhatsApp: {str(whatsapp_error)}")
                whatsapp_success = False

            # Mensagem final para interface
            final_message = f"""Perfeito, {user_name}! ✅

Suas informações foram registradas com sucesso e nossa equipe especializada em {area} foi notificada imediatamente.

Um advogado experiente do m.lima entrará em contato em breve para dar continuidade ao seu caso.

{'📱 Confirmação enviada no seu WhatsApp!' if whatsapp_success else '⚠️ Suas informações foram salvas, mas houve um problema ao enviar a confirmação no WhatsApp.'}

Obrigado por escolher nossos serviços jurídicos! 🤝"""

            logger.info(f"✅ DEBUG: Lead finalization completed successfully")
            return final_message
            
        except Exception as e:
            logger.error(f"❌ DEBUG: Error in lead finalization: {str(e)}")
            import traceback
            logger.error(f"❌ DEBUG: Finalization traceback: {traceback.format_exc()}")
            user_name = session_data.get("lead_data", {}).get("identification", "")
            return f"Obrigado pelas informações, {user_name}! Nossa equipe entrará em contato em breve."

    async def _handle_phone_collection(self, phone_message: str, session_id: str, session_data: Dict[str, Any]) -> str:
        """Coleta de telefone para casos onde não foi extraído automaticamente."""
        try:
            logger.info(f"📱 DEBUG: Phone collection for session {session_id}")
            phone_clean = ''.join(filter(str.isdigit, phone_message))
            logger.info(f"📱 DEBUG: Cleaned phone: {phone_clean}")
            
            if len(phone_clean) < 10 or len(phone_clean) > 13:
                logger.warning(f"❌ DEBUG: Invalid phone length: {len(phone_clean)}")
                return "Número inválido. Por favor, digite no formato com DDD (exemplo: 11999999999):"

            session_data["lead_data"]["phone"] = phone_clean
            return await self._handle_lead_finalization(session_id, session_data)
            
        except Exception as e:
            logger.error(f"❌ DEBUG: Error in phone collection: {str(e)}")
            user_name = session_data.get("lead_data", {}).get("identification", "")
            return f"Obrigado pelas informações, {user_name}! Nossa equipe entrará em contato em breve."

    async def process_message(self, message: str, session_id: str, phone_number: Optional[str] = None, platform: str = "web") -> Dict[str, Any]:
        """PROCESSAMENTO PRINCIPAL COM DEBUG COMPLETO"""
        try:
            logger.info(f"🎯 DEBUG: ===== PROCESS MESSAGE START =====")
            logger.info(f"🎯 DEBUG: Session: {session_id}, Platform: {platform}")
            logger.info(f"🎯 DEBUG: Message: '{message}'")
            logger.info(f"🎯 DEBUG: Phone number: {phone_number}")

            session_data = await self._get_or_create_session(session_id, platform, phone_number)
            
            current_step = session_data.get("fallback_step", "não iniciado")
            qualified = session_data.get("lead_qualified", False)
            phone_submitted = session_data.get("phone_submitted", False)
            flow_initialized = session_data.get("flow_initialized", False)
            
            logger.info(f"📊 DEBUG: Session state - Step: {current_step}, Qualified: {qualified}, Phone: {phone_submitted}, Flow init: {flow_initialized}")

            # Tratar coleta de telefone para leads qualificados sem telefone
            if (qualified and not phone_submitted and self._is_phone_number(message)):
                logger.info("📱 DEBUG: Processing phone collection for qualified lead")
                phone_response = await self._handle_phone_collection(message, session_id, session_data)
                return {
                    "response_type": "phone_collected_debug",
                    "platform": platform,
                    "session_id": session_id,
                    "response": phone_response,
                    "phone_submitted": True,
                    "message_count": session_data.get("message_count", 0) + 1
                }

            # USAR FLUXO ESTRUTURADO PARA TODAS AS PLATAFORMAS
            logger.info(f"🌐 DEBUG: Platform {platform} - Using structured flow with debug")
            
            fallback_response = await self._get_fallback_response(session_data, message)
            logger.info(f"📤 DEBUG: Fallback response: '{fallback_response[:100]}...'")
            
            # Atualizar sessão
            session_data["last_message"] = message
            session_data["last_response"] = fallback_response
            session_data["last_updated"] = ensure_utc(datetime.now(timezone.utc))
            session_data["message_count"] = session_data.get("message_count", 0) + 1
            await save_user_session(session_id, session_data)
            logger.info("💾 DEBUG: Session updated and saved")
            
            result = {
                "response_type": f"{platform}_fluxo_debug",
                "platform": platform,
                "session_id": session_id,
                "response": fallback_response,
                "ai_mode": False,
                "fallback_step": session_data.get("fallback_step"),
                "lead_qualified": session_data.get("lead_qualified", False),
                "fallback_completed": session_data.get("fallback_completed", False),
                "lead_data": session_data.get("lead_data", {}),
                "validation_attempts": session_data.get("validation_attempts", {}),
                "available_areas": ["Direito Penal", "Saúde/Liminares"],
                "message_count": session_data.get("message_count", 1),
                "session_started": session_data.get("session_started", False),
                "flow_initialized": session_data.get("flow_initialized", False)
            }
            
            logger.info(f"✅ DEBUG: Process message completed - Step: {result['fallback_step']}, Qualified: {result['lead_qualified']}")
            return result

        except Exception as e:
            logger.error(f"❌ DEBUG: Exception in process_message: {str(e)}")
            import traceback
            logger.error(f"❌ DEBUG: Process message traceback: {traceback.format_exc()}")
            return {
                "response_type": "orchestration_error_debug",
                "platform": platform,
                "session_id": session_id,
                "response": "Olá! Seja bem-vindo ao m.lima. Vamos iniciar, qual é o seu nome completo?",
                "error": str(e)
            }

    async def handle_phone_number_submission(self, phone_number: str, session_id: str) -> Dict[str, Any]:
        """Handle phone number submission from web interface."""
        try:
            logger.info(f"📱 DEBUG: Phone number submission for session {session_id}: {phone_number}")
            session_data = await get_user_session(session_id) or {}
            response = await self._handle_phone_collection(phone_number, session_id, session_data)
            return {
                "status": "success",
                "message": response,
                "phone_submitted": True,
                "flow_type": "fluxo_debug"
            }
        except Exception as e:
            logger.error(f"❌ DEBUG: Error in phone submission: {str(e)}")
            return {
                "status": "error",
                "message": "Erro ao processar número de WhatsApp",
                "error": str(e)
            }

    async def get_session_context(self, session_id: str) -> Dict[str, Any]:
        """Get current session context and status."""
        try:
            logger.info(f"📊 DEBUG: Getting session context for {session_id}")
            session_data = await get_user_session(session_id)
            if not session_data:
                logger.info(f"📊 DEBUG: No session found for {session_id}")
                return {"exists": False}

            context = {
                "exists": True,
                "session_id": session_id,
                "platform": session_data.get("platform", "unknown"),
                "fallback_step": session_data.get("fallback_step"),
                "lead_qualified": session_data.get("lead_qualified", False),
                "fallback_completed": session_data.get("fallback_completed", False),
                "phone_submitted": session_data.get("phone_submitted", False),
                "lead_data": session_data.get("lead_data", {}),
                "validation_attempts": session_data.get("validation_attempts", {}),
                "available_areas": ["Direito Penal", "Saúde/Liminares"],
                "flow_type": "fluxo_debug",
                "message_count": session_data.get("message_count", 0),
                "created_at": session_data.get("created_at"),
                "last_updated": session_data.get("last_updated"),
                "session_started": session_data.get("session_started", False),
                "flow_initialized": session_data.get("flow_initialized", False)
            }
            
            logger.info(f"📊 DEBUG: Session context: {context}")
            return context
        except Exception as e:
            logger.error(f"❌ DEBUG: Error getting session context: {str(e)}")
            return {"exists": False, "error": str(e)}


# Global instance
intelligent_orchestrator = IntelligentHybridOrchestrator()
hybrid_orchestrator = intelligent_orchestrator
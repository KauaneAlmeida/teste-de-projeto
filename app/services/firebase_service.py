"""
Firebase Service (com Secret Manager) - NOVO FLUXO DE QUALIFICAÇÃO

Este módulo gerencia a integração com o Firebase Admin SDK e operações no Firestore.
Agora o backend usa **exclusivamente** a variável de ambiente FIREBASE_KEY,
que deve conter o JSON completo da service account (via Secret Manager no Cloud Run).

NOVO FLUXO: 5 steps de qualificação (apenas Penal e Saúde/Liminares)
"""

import os
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, firestore
from fastapi import HTTPException, status

# Configure logging
logger = logging.getLogger(__name__)

# Global Firebase app instance
_firebase_app = None
_firestore_client = None


def initialize_firebase():
    """
    Inicializa o Firebase Admin SDK a partir da variável de ambiente FIREBASE_KEY.
    """
    global _firebase_app, _firestore_client

    if _firebase_app is not None:
        logger.info("✅ Firebase já inicializado")
        return

    try:
        firebase_key = os.getenv("FIREBASE_KEY")
        if not firebase_key:
            raise ValueError("Variável de ambiente FIREBASE_KEY não encontrada.")

        # Converte o JSON que veio da env em dict
        firebase_credentials = json.loads(firebase_key)
        cred = credentials.Certificate(firebase_credentials)

        logger.info("🔥 Inicializando Firebase com credenciais do Secret Manager")
        _firebase_app = firebase_admin.initialize_app(cred)
        _firestore_client = firestore.client()
        logger.info("✅ Firebase inicializado com sucesso")

    except Exception as e:
        logger.error(f"❌ Falha ao inicializar Firebase: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Falha na inicialização do Firebase: {str(e)}",
        )


def get_firestore_client():
    """
    Retorna a instância do cliente Firestore.
    """
    if _firestore_client is None:
        initialize_firebase()

    if _firestore_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Firestore client não disponível",
        )

    return _firestore_client


# --------------------------------------------------------------------------
# Conversation Flow - NOVO FLUXO DE QUALIFICAÇÃO
# --------------------------------------------------------------------------
async def get_conversation_flow() -> Dict[str, Any]:
    try:
        db = get_firestore_client()
        flow_ref = db.collection("conversation_flows").document("law_firm_intake")
        flow_doc = flow_ref.get()

        if not flow_doc.exists:
            logger.info("📝 Criando NOVO FLUXO de qualificação de leads")
            # NOVO FLUXO: 5 steps de qualificação (apenas Penal e Saúde)
            default_flow = {
                "steps": [
                    {
                        "id": 1, 
                        "question": "Olá! Seja bem-vindo ao m.lima. Estou aqui para entender seu caso e agilizar o contato com nossos advogados especializados.\n\nPara começar, qual é o seu nome completo?"
                    },
                    {
                        "id": 2, 
                        "question": "Prazer, {user_name}! Preciso do seu telefone/WhatsApp e e-mail para contato:"
                    },
                    {
                        "id": 3, 
                        "question": "Perfeito! Com qual área do direito você precisa de ajuda?\n\n• Penal\n• Saúde (ações e liminares médicas)"
                    },
                    {
                        "id": 4, 
                        "question": "Me conte brevemente sobre sua situação:\n\n• O caso já está em andamento?\n• Há prazos ou audiências?\n• Em qual cidade?"
                    },
                    {
                        "id": 5, 
                        "question": "Casos assim precisam de atenção imediata. Nossos advogados têm experiência em casos semelhantes.\n\nPosso direcioná-lo para nosso especialista?"
                    }
                ],
                "completion_message": "Perfeito! Nossa equipe do m.lima entrará em contato em breve.",
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "version": "2.0_novo_fluxo",
                "description": "Novo fluxo de qualificação de leads - 5 steps (apenas Penal e Saúde)",
                "areas": ["Direito Penal", "Saúde/Liminares"],
                "flow_type": "lead_qualification"
            }

            flow_ref.set(default_flow)
            logger.info("✅ NOVO FLUXO de qualificação criado (Penal + Saúde)")
            return default_flow

        flow_data = flow_doc.to_dict()
        steps = flow_data.get("steps", [])

        # Normaliza steps para o NOVO FLUXO
        normalized_steps = []
        for idx, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                normalized_steps.append({
                    "id": step.get("id", idx),
                    "question": step.get("question", ""),
                })
            else:
                normalized_steps.append({
                    "id": idx,
                    "question": str(step),
                })

        # NOVO FLUXO: Não precisa de step 0, inicia direto no step 1
        if not normalized_steps or not any(step.get("id") == 1 for step in normalized_steps):
            normalized_steps = [
                {
                    "id": 1,
                    "question": "Para começar, qual é o seu nome completo?"
                },
                {
                    "id": 2,
                    "question": "Prazer, {user_name}! Preciso do seu telefone/WhatsApp e e-mail:"
                },
                {
                    "id": 3,
                    "question": "Com qual área você precisa de ajuda? Penal ou Saúde (liminares)?"
                },
                {
                    "id": 4,
                    "question": "Me conte sobre sua situação: está em andamento? Há prazos? Qual cidade?"
                },
                {
                    "id": 5,
                    "question": "Casos assim precisam de atenção imediata. Posso direcioná-lo para nosso especialista?"
                }
            ]

        flow_data["steps"] = normalized_steps

        if "completion_message" not in flow_data:
            flow_data["completion_message"] = "Perfeito! Nossa equipe do m.lima entrará em contato em breve."

        # Adiciona informações do NOVO FLUXO
        flow_data["areas"] = ["Direito Penal", "Saúde/Liminares"]
        flow_data["flow_type"] = "lead_qualification"

        return flow_data

    except Exception as e:
        logger.error(f"❌ Erro ao buscar NOVO FLUXO de conversa: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao recuperar fluxo de conversa",
        )


# --------------------------------------------------------------------------
# Fallback Questions - NOVO FLUXO
# --------------------------------------------------------------------------
async def get_fallback_questions() -> list[str]:
    try:
        flow = await get_conversation_flow()
        steps = flow.get("steps", [])
        questions = [step["question"] for step in steps if "question" in step]
        logger.info(f"📝 NOVO FLUXO: {len(questions)} perguntas carregadas")
        return questions
    except Exception as e:
        logger.error(f"❌ Erro ao buscar perguntas do NOVO FLUXO: {e}")
        return [
            "Qual é o seu nome completo?",
            "Preciso do seu telefone/WhatsApp e e-mail:",
            "Com qual área você precisa de ajuda? Penal ou Saúde?",
            "Me conte sobre sua situação:",
            "Posso direcioná-lo para nosso especialista?"
        ]


# --------------------------------------------------------------------------
# Lead Management - NOVO FLUXO
# --------------------------------------------------------------------------
async def save_lead_data(lead_data: Dict[str, Any]) -> str:
    try:
        db = get_firestore_client()

        # NOVO FLUXO: Estrutura aprimorada para leads qualificados
        lead_doc = {
            "answers": lead_data.get("answers", []),
            "timestamp": datetime.now(),
            "status": "qualified_hot",  # NOVO FLUXO: leads são qualificados
            "source": "novo_fluxo_qualificacao",
            "flow_type": "lead_qualification",
            "areas_available": ["Direito Penal", "Saúde/Liminares"],
            "lead_temperature": "hot",
            "urgency": "high",
            "created_at": datetime.now(),
            "updated_at": datetime.now(),
        }

        # Adiciona resumo se disponível
        if "lead_summary" in lead_data:
            lead_doc["lead_summary"] = lead_data["lead_summary"]

        leads_ref = db.collection("leads")
        doc_ref = leads_ref.add(lead_doc)
        lead_id = doc_ref[1].id
        logger.info(f"💾 NOVO FLUXO: Lead qualificado salvo com ID: {lead_id}")
        return lead_id

    except Exception as e:
        logger.error(f"❌ Erro ao salvar lead do NOVO FLUXO: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao salvar lead qualificado",
        )


async def update_lead_data(lead_id: str, update_data: Dict[str, Any]) -> bool:
    try:
        db = get_firestore_client()
        update_data["updated_at"] = datetime.now()
        
        # NOVO FLUXO: Adiciona metadados de atualização
        if "flow_type" not in update_data:
            update_data["flow_type"] = "lead_qualification"
        
        db.collection("leads").document(lead_id).update(update_data)
        logger.info(f"📝 NOVO FLUXO: Lead {lead_id} atualizado")
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao atualizar lead {lead_id}: {str(e)}")
        return False


# --------------------------------------------------------------------------
# Session Management - NOVO FLUXO
# --------------------------------------------------------------------------
async def get_user_session(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        db = get_firestore_client()
        doc = db.collection("user_sessions").document(session_id).get()
        session_data = doc.to_dict() if doc.exists else None
        
        if session_data:
            # NOVO FLUXO: Adiciona metadados se não existirem
            if "flow_type" not in session_data:
                session_data["flow_type"] = "lead_qualification"
            if "available_areas" not in session_data:
                session_data["available_areas"] = ["Direito Penal", "Saúde/Liminares"]
                
        return session_data
    except Exception as e:
        logger.error(f"❌ Erro ao buscar sessão {session_id}: {str(e)}")
        return None


async def save_user_session(session_id: str, session_data: Dict[str, Any]) -> bool:
    try:
        db = get_firestore_client()
        
        # NOVO FLUXO: Adiciona metadados da sessão
        session_data["last_updated"] = datetime.now()
        if "created_at" not in session_data:
            session_data["created_at"] = datetime.now()
        if "flow_type" not in session_data:
            session_data["flow_type"] = "lead_qualification"
        if "available_areas" not in session_data:
            session_data["available_areas"] = ["Direito Penal", "Saúde/Liminares"]
        
        db.collection("user_sessions").document(session_id).set(session_data, merge=True)
        return True
    except Exception as e:
        logger.error(f"❌ Erro ao salvar sessão {session_id}: {str(e)}")
        return False


# --------------------------------------------------------------------------
# Qualified Leads Management - NOVO FLUXO
# --------------------------------------------------------------------------
async def get_qualified_leads(limit: int = 50) -> list[Dict[str, Any]]:
    """Busca leads qualificados do NOVO FLUXO."""
    try:
        db = get_firestore_client()
        
        query = db.collection("leads")\
                 .where("status", "==", "qualified_hot")\
                 .where("flow_type", "==", "lead_qualification")\
                 .order_by("created_at", direction=firestore.Query.DESCENDING)\
                 .limit(limit)
        
        docs = query.get()
        leads = []
        
        for doc in docs:
            lead_data = doc.to_dict()
            lead_data["id"] = doc.id
            leads.append(lead_data)
        
        logger.info(f"📊 NOVO FLUXO: {len(leads)} leads qualificados encontrados")
        return leads
        
    except Exception as e:
        logger.error(f"❌ Erro ao buscar leads qualificados: {str(e)}")
        return []


async def mark_lead_contacted(lead_id: str, lawyer_info: Dict[str, Any] = None) -> bool:
    """Marca lead como contatado."""
    try:
        update_data = {
            "status": "contacted",
            "contacted_at": datetime.now(),
            "updated_at": datetime.now()
        }
        
        if lawyer_info:
            update_data["assigned_lawyer"] = lawyer_info
        
        return await update_lead_data(lead_id, update_data)
        
    except Exception as e:
        logger.error(f"❌ Erro ao marcar lead {lead_id} como contatado: {str(e)}")
        return False


# --------------------------------------------------------------------------
# Health Check - NOVO FLUXO
# --------------------------------------------------------------------------
async def get_firebase_service_status() -> Dict[str, Any]:
    try:
        db = get_firestore_client()
        
        # Test basic connectivity
        try:
            test_collection = db.collection("conversation_flows").limit(1)
            _ = test_collection.get()
            logger.info("✅ Firebase Firestore connection test successful")
        except Exception as read_error:
            logger.error(f"❌ Firebase Firestore connection test failed: {str(read_error)}")
            raise read_error

        # Test NOVO FLUXO collections
        try:
            leads_count = len(db.collection("leads").where("flow_type", "==", "lead_qualification").limit(1).get())
            sessions_count = len(db.collection("user_sessions").limit(1).get())
        except:
            leads_count = 0
            sessions_count = 0

        return {
            "service": "firebase_service_novo_fluxo",
            "status": "active",
            "firestore_connected": True,
            "credentials_source": "env:FIREBASE_KEY",
            "collections": {
                "conversation_flows": "active",
                "leads": f"active ({leads_count} qualified leads)",
                "user_sessions": f"active ({sessions_count} sessions)",
                "_health_check": "active"
            },
            "flow_type": "lead_qualification",
            "available_areas": ["Direito Penal", "Saúde/Liminares"],
            "message": "Firebase Firestore operational with NOVO FLUXO",
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"❌ Firebase health check failed: {str(e)}")
        return {
            "service": "firebase_service_novo_fluxo",
            "status": "error",
            "firestore_connected": False,
            "error": str(e),
            "configuration_required": True,
            "message": f"Firebase connection failed: {str(e)}",
            "timestamp": datetime.now().isoformat()
        }


# Inicializa no import
try:
    initialize_firebase()
    logger.info("🔥 Módulo Firebase service (NOVO FLUXO) carregado com sucesso")
except Exception as e:
    logger.warning(f"⚠️ Inicialização adiada do Firebase: {str(e)}")
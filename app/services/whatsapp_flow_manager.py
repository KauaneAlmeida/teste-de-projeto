"""
WhatsApp Flow Manager

Manages two distinct WhatsApp lead flows:
1. Landing Chat Completion Flow - Single confirmation message after chatbot completion
2. Direct WhatsApp Flow - Continuous conversation after WhatsApp button click
"""

import logging
import urllib.parse
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from app.services.baileys_service import baileys_service
from app.services.lead_assignment_service import lead_assignment_service

logger = logging.getLogger(__name__)


class WhatsAppFlowManager:
    """Manages different WhatsApp lead flows based on user interaction source."""
    
    def __init__(self):
        self.flow_types = {
            "landing_chat_completion": "Flow 1 - Single confirmation message",
            "direct_whatsapp": "Flow 2 - Continuous conversation"
        }
    
    async def handle_landing_chat_completion(
        self,
        user_data: Dict[str, Any],
        phone_number: str
    ) -> Dict[str, Any]:
        """
        Flow 1: Handle completion of landing page chatbot
        - Send single confirmation message to user
        - Notify lawyers with assignment links
        - Do NOT continue conversation
        """
        try:
            logger.info(f"🎯 Flow 1: Landing chat completion for {phone_number}")
            
            # Extract user information
            user_name = user_data.get("name", "Cliente")
            area = user_data.get("area", "não informada")
            situation = user_data.get("situation", "não detalhada")
            email = user_data.get("email", "não informado")
            
            # Format phone for WhatsApp
            whatsapp_number = self._format_phone_for_whatsapp(phone_number)
            
            # Create single confirmation message
            confirmation_message = f"""Olá {user_name}! 👋

Recebemos suas informações do nosso site e nossa equipe especializada em {area} foi notificada.

📋 Resumo:
• Nome: {user_name}
• Área: {area}
• Situação: {situation[:100]}{'...' if len(situation) > 100 else ''}
• Email: {email}

Um advogado experiente entrará em contato em breve.

Obrigado por escolher nossos serviços! 🤝"""

            # Send confirmation message
            message_sent = await baileys_service.send_whatsapp_message(
                whatsapp_number, 
                confirmation_message
            )
            
            if not message_sent:
                logger.error(f"❌ Failed to send confirmation message to {phone_number}")
            
            # Notify lawyers with assignment links
            notification_result = await lead_assignment_service.create_lead_with_assignment_links(
                lead_name=user_name,
                lead_phone=phone_number,
                category=area,
                situation=situation,
                additional_data={
                    "email": email,
                    "source": "landing_chat_completion",
                    "flow_type": "single_confirmation",
                    "conversation_allowed": False,
                    "completed_at": datetime.now(timezone.utc).isoformat()
                }
            )
            
            logger.info(f"✅ Flow 1 completed for {user_name} - {phone_number}")
            
            return {
                "success": True,
                "flow_type": "landing_chat_completion",
                "message_sent": message_sent,
                "lawyers_notified": notification_result.get("success", False),
                "conversation_continues": False,
                "user_name": user_name,
                "phone_number": phone_number
            }
            
        except Exception as e:
            logger.error(f"❌ Error in Flow 1 for {phone_number}: {str(e)}")
            return {
                "success": False,
                "flow_type": "landing_chat_completion",
                "error": str(e),
                "conversation_continues": False
            }
    
    async def handle_direct_whatsapp_authorization(
        self,
        session_id: str,
        phone_number: str,
        source: str = "whatsapp_button"
    ) -> Dict[str, Any]:
        """
        Flow 2: Handle direct WhatsApp button clicks
        - Authorize session for continuous conversation
        - Set up for structured conversation flow
        - Return WhatsApp URL with predefined message
        """
        try:
            logger.info(f"🎯 Flow 2: Direct WhatsApp authorization for {session_id}")
            
            # Create authorization data for continuous conversation
            authorization_data = {
                "session_id": session_id,
                "phone_number": phone_number,
                "source": source,
                "flow_type": "direct_whatsapp",
                "conversation_allowed": True,
                "authorized_at": datetime.now(timezone.utc).isoformat(),
                "predefined_message_sent": False
            }
            
            # Generate predefined message for WhatsApp
            predefined_message = self._get_predefined_message(source)
            
            # Create WhatsApp URL with predefined message
            whatsapp_url = self._create_whatsapp_url(phone_number, predefined_message)
            
            logger.info(f"✅ Flow 2 authorization completed for {session_id}")
            
            return {
                "success": True,
                "flow_type": "direct_whatsapp",
                "session_id": session_id,
                "phone_number": phone_number,
                "whatsapp_url": whatsapp_url,
                "predefined_message": predefined_message,
                "conversation_continues": True,
                "authorization_data": authorization_data
            }
            
        except Exception as e:
            logger.error(f"❌ Error in Flow 2 authorization for {session_id}: {str(e)}")
            return {
                "success": False,
                "flow_type": "direct_whatsapp",
                "error": str(e),
                "conversation_continues": False
            }
    
    def _format_phone_for_whatsapp(self, phone: str) -> str:
        """Format phone number for WhatsApp messaging."""
        try:
            # Clean phone number
            clean_phone = ''.join(filter(str.isdigit, phone))
            
            # Add Brazil country code if not present
            if not clean_phone.startswith("55"):
                clean_phone = f"55{clean_phone}"
            
            # Ensure proper mobile format
            if len(clean_phone) == 12:  # 55 + 10 digits
                # Add 9th digit for mobile
                area_code = clean_phone[2:4]
                number = clean_phone[4:]
                if len(number) == 8 and number[0] in ['6', '7', '8', '9']:
                    clean_phone = f"55{area_code}9{number}"
            
            return f"{clean_phone}@s.whatsapp.net"
            
        except Exception as e:
            logger.error(f"❌ Error formatting phone {phone}: {str(e)}")
            return f"5511999999999@s.whatsapp.net"  # Fallback
    
    def _get_predefined_message(self, source: str) -> str:
        """Get predefined message based on source button."""
        messages = {
            "whatsapp_24h": "Olá! Vi que vocês atendem 24h. Preciso de ajuda jurídica urgente.",
            "whatsapp_floating": "Olá! Gostaria de falar com um advogado sobre meu caso.",
            "whatsapp_specialist": "Olá! Quero falar com um especialista sobre minha situação jurídica.",
            "whatsapp_button": "Olá! Gostaria de conversar sobre serviços jurídicos.",
            "whatsapp_cta": "Olá! Vi o site de vocês e gostaria de mais informações."
        }
        
        return messages.get(source, "Olá! Gostaria de falar sobre serviços jurídicos.")
    
    def _create_whatsapp_url(self, phone_number: str, message: str) -> str:
        """Create WhatsApp URL with predefined message."""
        try:
            # Clean phone number for URL
            clean_phone = ''.join(filter(str.isdigit, phone_number))
            if not clean_phone.startswith("55"):
                clean_phone = f"55{clean_phone}"
            
            # URL encode the message
            encoded_message = urllib.parse.quote(message)
            
            return f"https://wa.me/{clean_phone}?text={encoded_message}"
            
        except Exception as e:
            logger.error(f"❌ Error creating WhatsApp URL: {str(e)}")
            return f"https://wa.me/5511999999999"


# Global instance
whatsapp_flow_manager = WhatsAppFlowManager()
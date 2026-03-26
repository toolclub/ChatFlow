<script setup lang="ts">
import { onMounted } from 'vue'
import { useChat } from './composables/useChat'
import Sidebar from './components/Sidebar.vue'
import ChatView from './components/ChatView.vue'

const chat = useChat()

onMounted(async () => {
  await chat.loadConversations()
  await chat.restoreFromHash()
})
</script>

<template>
  <div class="app">
    <Sidebar
      :conversations="chat.conversations.value"
      :currentConvId="chat.currentConvId.value"
      @new-chat="chat.newConversation()"
      @select="chat.selectConversation($event)"
      @delete="chat.removeConversation($event)"
    />
    <ChatView
      :messages="chat.messages.value"
      :loading="chat.loading.value"
      :agentStatus="chat.agentStatus.value"
      @send="chat.send($event)"
    />
  </div>
</template>

package com.kyhsgeekcode.minecraftenv

import com.kyhsgeekcode.minecraftenv.proto.ActionSpace
import net.minecraft.client.MinecraftClient
import org.lwjgl.glfw.GLFW.GLFW_KEY_1
import org.lwjgl.glfw.GLFW.GLFW_KEY_2
import org.lwjgl.glfw.GLFW.GLFW_KEY_3
import org.lwjgl.glfw.GLFW.GLFW_KEY_4
import org.lwjgl.glfw.GLFW.GLFW_KEY_5
import org.lwjgl.glfw.GLFW.GLFW_KEY_6
import org.lwjgl.glfw.GLFW.GLFW_KEY_7
import org.lwjgl.glfw.GLFW.GLFW_KEY_8
import org.lwjgl.glfw.GLFW.GLFW_KEY_9
import org.lwjgl.glfw.GLFW.GLFW_KEY_A
import org.lwjgl.glfw.GLFW.GLFW_KEY_D
import org.lwjgl.glfw.GLFW.GLFW_KEY_E
import org.lwjgl.glfw.GLFW.GLFW_KEY_LEFT_CONTROL
import org.lwjgl.glfw.GLFW.GLFW_KEY_LEFT_SHIFT
import org.lwjgl.glfw.GLFW.GLFW_KEY_Q
import org.lwjgl.glfw.GLFW.GLFW_KEY_S
import org.lwjgl.glfw.GLFW.GLFW_KEY_SPACE
import org.lwjgl.glfw.GLFW.GLFW_KEY_W
import org.lwjgl.glfw.GLFW.GLFW_PRESS
import org.lwjgl.glfw.GLFW.GLFW_RELEASE
import org.lwjgl.glfw.GLFWCharModsCallbackI
import org.lwjgl.glfw.GLFWKeyCallbackI

object KeyboardInfo {
    var charModsCallback: GLFWCharModsCallbackI? = null
    var keyCallback: GLFWKeyCallbackI? = null
    var handle: Long = 0

    var currentState: MutableMap<Int, Boolean> = mutableMapOf()

    val keyMappings =
        mapOf(
            "W" to GLFW_KEY_W,
            "A" to GLFW_KEY_A,
            "S" to GLFW_KEY_S,
            "D" to GLFW_KEY_D,
            "LShift" to GLFW_KEY_LEFT_SHIFT,
            "Ctrl" to GLFW_KEY_LEFT_CONTROL,
            "Space" to GLFW_KEY_SPACE,
            "E" to GLFW_KEY_E,
            "Q" to GLFW_KEY_Q,
//        "F" to GLFW_KEY_F,
            "Hotbar1" to GLFW_KEY_1,
            "Hotbar2" to GLFW_KEY_2,
            "Hotbar3" to GLFW_KEY_3,
            "Hotbar4" to GLFW_KEY_4,
            "Hotbar5" to GLFW_KEY_5,
            "Hotbar6" to GLFW_KEY_6,
            "Hotbar7" to GLFW_KEY_7,
            "Hotbar8" to GLFW_KEY_8,
            "Hotbar9" to GLFW_KEY_9,
        )

    fun onAction(
        actionDict: ActionSpace.ActionSpaceMessageV2,
        handleInventory: Boolean = true,
    ) {
        val actions =
            mapOf(
                "W" to actionDict.forward,
                "A" to actionDict.left,
                "S" to actionDict.back,
                "D" to actionDict.right,
                "LShift" to actionDict.sneak,
                "Ctrl" to actionDict.sprint,
                "Space" to actionDict.jump,
                "E" to (actionDict.inventory && handleInventory),
                "Q" to actionDict.drop,
//            "F" to actionDict.swapHands,
                "Hotbar1" to actionDict.hotbar1,
                "Hotbar2" to actionDict.hotbar2,
                "Hotbar3" to actionDict.hotbar3,
                "Hotbar4" to actionDict.hotbar4,
                "Hotbar5" to actionDict.hotbar5,
                "Hotbar6" to actionDict.hotbar6,
                "Hotbar7" to actionDict.hotbar7,
                "Hotbar8" to actionDict.hotbar8,
                "Hotbar9" to actionDict.hotbar9,
            )

        val client = MinecraftClient.getInstance()
        for ((key, glfwKey) in keyMappings) {
            val previousState = currentState[glfwKey] ?: false
            val currentState = actions[key] ?: false

            if (!previousState && currentState) {
                this.currentState[glfwKey] = true
                client.keyboard.onKey(handle, glfwKey, 0, GLFW_PRESS, 0)
            } else if (previousState && !currentState) {
                this.currentState[glfwKey] = false
                client.keyboard.onKey(handle, glfwKey, 0, GLFW_RELEASE, 0)
            }
        }
    }

    fun isKeyPressed(key: Int): Boolean = currentState[key] ?: false
}

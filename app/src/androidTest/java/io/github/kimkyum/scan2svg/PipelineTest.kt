package io.github.kimkyum.scan2svg

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/** 기기(에뮬레이터)에서 실제로 예시 사진을 변환해 본다. 결과물은 앱의 out 폴더에 남겨 CI가 꺼내 간다. */
@RunWith(AndroidJUnit4::class)
class PipelineTest {
    @Test
    fun convertsSamplePhoto() {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val out = Pipeline.outDir(ctx)
        val log = StringBuilder()
        log.appendLine("versions: " + Pipeline.versions(ctx))
        File(out, "log.txt").writeText(log.toString())

        val input = File(ctx.cacheDir, "sample.jpg")
        ctx.assets.open("sample_photo.jpg").use { i -> input.outputStream().use { i.copyTo(it) } }
        val r = Pipeline.run(ctx, input) { log.appendLine(it) }
        log.appendLine(if (r.ok) "OK" else "FAILED: " + r.message)
        File(out, "log.txt").writeText(log.toString())

        assertTrue(r.message, r.ok)
        val svg = r.svg.readText()
        assertTrue("SVG에 글자 요소가 없다", svg.contains("<text"))
    }
}

"""
分布式 Agent 调度器的命令行入口

使用方式：
    python distributed_main.py "你的查询"
    或者直接运行交互式输入
"""
import sys
import asyncio
from src.distributed_workflow import run_distributed_workflow


async def main():
    """主函数"""
    print("=" * 60)
    print("🚀 分布式 Agent 调度器 (Ubiquitous Agent System - L2)")
    print("=" * 60)
    print()
    
    # 获取用户输入
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:])
    else:
        user_query = input("💬 请输入你的请求: ")
    
    if not user_query.strip():
        print("❌ 输入不能为空")
        sys.exit(1)
    
    print(f"\n📝 用户请求: {user_query}")
    print("-" * 60)
    
    # 运行工作流
    try:
        result = await run_distributed_workflow(
            user_input=user_query,
            debug=True,
            max_retries=3,
            timeout_seconds=30,
            adaptive_mode=True  # ← 启用自适应编排
        )
        
        # 显示编排模式信息
        print("\n" + "=" * 60)
        print("🎯 执行概览")
        print("=" * 60)
        orchestration_mode = result.get("orchestration_mode", "unknown")
        complexity_level = result.get("complexity_level", "unknown")
        
        mode_names = {
            "sequential": "Sequential (串行)",
            "concurrent": "Concurrent (并行)",
            "magentic": "Magentic-One (动态反馈)"
        }
        
        print(f"复杂度: {complexity_level.upper()} | 模式: {mode_names.get(orchestration_mode, orchestration_mode)}")
        
        # 打印执行计划摘要
        if result.get("execution_plan"):
            print(f"任务数: {len(result['execution_plan'])}")
            completed = sum(1 for t in result["execution_plan"] if t["status"] == "completed")
            print(f"完成: {completed}/{len(result['execution_plan'])}")
            print()
            
            for task in result["execution_plan"]:
                status_icon = "✅" if task["status"] == "completed" else "❌"
                print(f"{status_icon} {task['task_title']} → {task['assigned_agent_id']}")
        
        # 只打印最终结果
        print("\n" + "=" * 60)
        print("📄 最终结果")
        print("=" * 60)
        
        # 找到最后一个 reporter 或 executor 的消息
        final_message = None
        for message in reversed(result["messages"]):
            role = getattr(message, 'name', None)
            if role in ["reporter", "executor"]:
                final_message = message
                break
        
        if final_message:
            # 只显示结果的前500字符，如果太长就截断
            content = final_message.content
            if len(content) > 1000:
                print(content[:1000])
                print("\n... (内容过长，已截断)")
                print(f"\n💡 提示: 完整结果共 {len(content)} 字符")
            else:
                print(content)
        
        print("=" * 60)
        print("✨ 执行完成")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

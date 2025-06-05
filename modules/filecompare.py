import difflib
from collections import defaultdict
from colorama import Fore, init, Style


def check_conflicts(fdict_local, fdict_torrent):
    """
    检测同名文件不同大小的冲突情况
    
    Returns:
        bool: 如果存在冲突返回True，否则返回False
    """
    for name, size in fdict_torrent.items():
        if name in fdict_local:
            if fdict_local[name] != size:
                print(f"{Fore.RED}File conflict detected! File: {name}, "
                            f"Local size: {fdict_local[name]}, "
                            f"Torrent size: {size}{Style.RESET_ALL}")
                return True
    return False

def generate_rename_map(fdict_local, fdict_torrent):
    """
    生成从fdict_local到fdict_torrent的重命名映射
    
    Returns:
        dict: 重命名映射字典，格式如 {"1.flac": "1-1.flac", "2.flac": "1-2.flac"}
    """
    # 步骤1: 创建剩余文件字典（移除同名文件）
    remaining_local = fdict_local.copy()
    remaining_torrent = fdict_torrent.copy()
    
    # 移除同名文件（无论大小是否相同）
    for name in fdict_torrent.keys():
        if name in fdict_local:
            del remaining_local[name]
            del remaining_torrent[name]
    
    # 按文件大小分组本地文件
    size_map_local = defaultdict(list)
    for name, size in remaining_local.items():
        size_map_local[size].append(name)
    
    # 初始化重命名映射
    rename_map = {}
    
    # 遍历种子文件字典
    for torrent_name, torrent_size in list(remaining_torrent.items()):
        # 检查是否有相同大小的本地文件
        if torrent_size in size_map_local:
            local_names = size_map_local[torrent_size]
            
            if len(local_names) == 1:
                # 唯一匹配：直接建立映射
                local_name = local_names[0]
                rename_map[torrent_name] = local_name
                
                # 移除已匹配的文件
                del remaining_torrent[torrent_name]
                del size_map_local[torrent_size]  # 整个大小组已匹配
            
            elif len(local_names) > 1:
                # 多个相同大小的文件：使用文件名相似度匹配
                best_match = None
                best_similarity = -1
                
                # 计算相似度找到最佳匹配
                for local_name in local_names:
                    similarity = difflib.SequenceMatcher(
                        None, local_name, torrent_name
                    ).ratio()
                    
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_match = local_name
                
                # 如果找到匹配项
                if best_match:
                    rename_map[torrent_name] = best_match
                    
                    # 移除已匹配的文件
                    del remaining_torrent[torrent_name]
                    size_map_local[torrent_size].remove(best_match)
                    
                    # 如果该大小组已空，移除整个组
                    if not size_map_local[torrent_size]:
                        del size_map_local[torrent_size]
    
    return process_rename_map_for_transmission(rename_map)


def process_rename_map_for_transmission(rename_map):
    """
    处理重命名映射以适应Transmission
    """
    transmission_map = {}
    temp_map = {}
    for torrent_name, local_name in rename_map.items():
        torrent_name_list = torrent_name.split('/')
        local_name_list = local_name.split('/')
        # 不是同级的移动transmission不能完成
        if len(torrent_name_list) == len(local_name_list):
            for i in range(len(torrent_name_list)):
                if torrent_name_list[i] != local_name_list[i]:
                    temp_map[('/'.join(torrent_name_list[:i+1]), local_name_list[i])] = i
    
    for (key, value), priority in sorted(temp_map.items(), key=lambda item: item[1], reverse=True):
        transmission_map[key] = value
        
    return transmission_map